"""The real LLM clients behind `LLMClient` (Section 11.2): the plug that lets the director (S7), custom props (S8) and
the revision planner talk to a model on the internet.

The plan puts these adapters in the shared milestone M1; they are written here (اجتهاد) because Hareth asked for module
6 to work end to end. Two providers, as Section 11.1 names them:

- `anthropic`          Claude, with ANTHROPIC_API_KEY.
- `openai_compatible`  any server that speaks the OpenAI chat API, with OPENAI_BASE_URL and OPENAI_API_KEY:
                       Groq (https://api.groq.com/openai/v1),
                       Gemini (https://generativelanguage.googleapis.com/v1beta/openai), DeepSeek, OpenRouter…

How a call works (Section 14, the fallback for structured output): the system prompt asks for one JSON object and
shows the JSON Schema; the answer is parsed and checked against the schema; if it is not JSON or does not match, the
model gets ONE repair attempt with the problems listed (Section 12.2). An answer that is JSON but still off the schema
is returned, so the stage's own checks count it as a failed attempt and send their feedback (Section 5.0). Transient
errors (429, 5xx, timeouts) are retried by core/retry.py; anything else is a ProviderError that never holds the key.
Usage (tokens in/out, latency) is kept in `usage` and logged without any prompt text (Section 12.4).

`from_env()` gives the configured client, or None when no key is set: module 6 then runs exactly as before (the ready
story folders, whose scene plans are written by hand)."""

from __future__ import annotations

import base64
import json
import logging
import os
import random
import re
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping

from ...core.retry import BACKOFF_S, ProviderError, TransientError
from . import LLMClient

log = logging.getLogger("story_service.llm")

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MODELS = {"large": "claude-sonnet-5-5", "small": "claude-haiku-5-5", "vision": "claude-sonnet-5-5"}  # 11.1
TRIES = 5  # Section 12.2: at most 5 tries for transient errors
MAX_WAIT_S = 60  # a Retry-After longer than this is cut to it
MIN_TOKENS_OPENAI = 1024  # reasoning models spend tokens before answering; a 200-token budget leaves no answer
JSON_RULE = (
    "\n\n## Output format\nReply with ONE JSON object only: no prose, no markdown fences. "
    "It must match this JSON Schema:\n"
)
REPAIR = (
    "Your previous reply could not be used: {problem}\n"
    "Reply again with ONE JSON object only that matches the JSON Schema in the instructions.\n"
    "<previous_reply>\n{previous}\n</previous_reply>"
)

# post(url, headers, json_body, timeout) -> (status_code, response_text[, retry_after_seconds]).
# The default uses httpx; tests pass a fake.
Post = Callable[[str, dict, dict, float], tuple]


class RequestRefused(ProviderError):
    """The provider answered 4xx (not 429): `.status` and `.body` (its text) say why. Never holds the key."""

    def __init__(self, provider: str, status: int, body: str):
        super().__init__(f"{provider} {status}: {_short(body)}")
        self.status, self.body = status, body or ""


class Busy(TransientError):
    """429 or 5xx; `.retry_after` is the provider's Retry-After in seconds, when it gave one."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


def httpx_post(url: str, headers: dict, body: dict, timeout: float) -> tuple[int, str, float | None]:
    import httpx  # imported here so the fakes and the tests need no HTTP library

    try:
        r = httpx.post(url, headers=headers, json=body, timeout=timeout)
    except (httpx.TimeoutException, httpx.TransportError) as e:
        raise TransientError(type(e).__name__) from None
    try:
        retry_after = float(r.headers.get("retry-after", ""))
    except ValueError:
        retry_after = None
    return r.status_code, r.text, retry_after


def parse_json(text: str, schema: dict | None) -> tuple[dict | None, str | None]:
    """(object, None) when `text` holds a JSON object that matches `schema`; (object or None, problem) otherwise.
    Markdown fences and text around the object are tolerated."""
    t = (text or "").strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", t, re.S)
    if fence:
        t = fence.group(1)
    try:
        data = json.loads(t)
    except json.JSONDecodeError:
        start, end = t.find("{"), t.rfind("}")
        try:
            data = json.loads(t[start : end + 1]) if 0 <= start < end else None
        except json.JSONDecodeError:
            data = None
    if not isinstance(data, dict):
        return None, "it was not one JSON object" + (" (the reply was empty)" if not t else "")
    if schema:
        from jsonschema import Draft202012Validator

        errors = sorted(Draft202012Validator(schema).iter_errors(data), key=lambda e: list(e.absolute_path))
        if errors:
            where = ["/".join(map(str, e.absolute_path)) or "(top)" for e in errors[:6]]
            listed = "; ".join(f"{w}: {e.message[:160]}" for w, e in zip(where, errors[:6]))
            return data, f"it does not match the schema ({len(errors)} problem(s)): {listed}"
    return data, None


class JsonClient(LLMClient):
    """What both providers share: the JSON rule, the repair attempt, retries, the concurrency limit and usage."""

    provider = "?"
    timeout_s = 120.0  # Section 11.3: LLM and VLM calls 120 s

    def __init__(self, models: Mapping[str, str], *, max_concurrency: int = 8, post: Post | None = None):
        self.models = dict(models)
        self.post = post or httpx_post
        self.sleep = time.sleep  # the waits between retries; tests replace it
        self._slots = threading.BoundedSemaphore(max(1, max_concurrency))
        self.usage: deque[dict] = deque(maxlen=500)

    def describe(self) -> str:
        return f"{self.provider} · {self.models['large']}"

    def complete_json(self, system, user, schema, model_role, temperature, max_tokens, images=None) -> dict:
        """The parsed answer. `{}` when the model gives no usable JSON even after the repair attempt, or when an image
        must be judged and no model that sees is configured: the stage's own checks then count a failed attempt
        (S7 asks again with feedback, S8 falls back to the generic prop)."""
        if images and not self.models.get("vision"):
            log.info("no vision model configured: the image is not judged")
            return {}
        model = self.models.get(model_role) or self.models["large"]
        system_full = system + JSON_RULE + json.dumps(schema, ensure_ascii=False)
        text = self._call_or_none(model, model_role, system_full, user, temperature, max_tokens, images)
        if text is None:
            return {}
        data, problem = parse_json(text, schema)
        if problem is None:
            return data
        log.info("llm reply unusable, one repair attempt (%s, %s)", model_role, problem[:120])
        again = user + "\n\n" + REPAIR.format(problem=problem, previous=(text or "")[:6000])
        text = self._call_or_none(model, model_role, system_full, again, temperature, max_tokens, images)
        data, _ = parse_json(text or "", schema)
        if data is None:
            log.warning("llm gave no JSON object after the repair attempt (%s %s)", self.provider, model)
            return {}
        return data  # JSON, maybe still off the schema: the stage's checks decide

    def _call_or_none(self, model, role, system, user, temperature, max_tokens, images) -> str | None:
        try:
            return self._call(model, role, system, user, temperature, max_tokens, images)
        except RequestRefused as e:
            if images:  # a model that cannot take this image: the drawing is not judged
                log.info("image refused by %s (%s): not judged", model, e.status)
                return None
            raise

    def _call(self, model, role, system, user, temperature, max_tokens, images) -> str:
        with self._slots:
            started = time.monotonic()
            for attempt in range(TRIES):
                try:
                    text, tokens_in, tokens_out = self._send(model, system, user, temperature, max_tokens, images)
                    break
                except TransientError as e:
                    if attempt == TRIES - 1:
                        raise
                    wait = getattr(e, "retry_after", None) or BACKOFF_S[attempt] * random.uniform(0.8, 1.2)
                    self.sleep(min(wait, MAX_WAIT_S))
        entry = {
            "provider": self.provider,
            "model": model,
            "role": role,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "latency_ms": int((time.monotonic() - started) * 1000),
        }
        self.usage.append(entry)
        log.info("llm call %s", entry)
        return text

    def _post(self, url: str, headers: dict, body: dict) -> dict:
        res = self.post(url, headers, body, self.timeout_s)
        status, text = res[0], res[1]
        if status == 429 or status >= 500:
            raise Busy(f"{self.provider} {status}", res[2] if len(res) > 2 else None)
        if status != 200:
            raise RequestRefused(self.provider, status, text)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            raise ProviderError(f"{self.provider}: the answer was not JSON: {_short(text)}") from None

    def _send(self, model, system, user, temperature, max_tokens, images) -> tuple[str, int | None, int | None]:
        raise NotImplementedError


def _short(text: str) -> str:
    return re.sub(r"\s+", " ", text or "")[:300]


def _b64(png: bytes) -> str:
    return base64.b64encode(png).decode("ascii")


class AnthropicClient(JsonClient):
    """Claude through the Messages API."""

    provider = "anthropic"

    def __init__(self, api_key: str, models: Mapping[str, str], **kw):
        if not api_key:
            raise ProviderError("ANTHROPIC_API_KEY is not set")
        super().__init__(models, **kw)
        self._key = api_key

    def __repr__(self) -> str:
        return f"AnthropicClient({self.models['large']!r})"  # never the key

    def _send(self, model, system, user, temperature, max_tokens, images):
        content = [{"type": "text", "text": user}]
        content += [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": _b64(png)}}
            for png in images or []
        ]
        body = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": content}],
            "temperature": temperature,
        }
        headers = {"x-api-key": self._key, "anthropic-version": ANTHROPIC_VERSION, "content-type": "application/json"}
        out = self._post(ANTHROPIC_URL, headers, body)
        if out.get("stop_reason") == "max_tokens":
            log.warning("anthropic reply cut at max_tokens=%s", max_tokens)
        text = "".join(
            b.get("text", "") for b in out.get("content") or [] if isinstance(b, dict) and b.get("type") == "text"
        )
        usage = out.get("usage") or {}
        return text, usage.get("input_tokens"), usage.get("output_tokens")


class OpenAICompatibleClient(JsonClient):
    """Any OpenAI-style chat completions server. JSON mode (`response_format: json_object`) is asked for; a server
    that refuses a parameter (JSON mode, `max_tokens`, `temperature`) gets the request again without it, once each.
    Groq's `json_validate_failed` (JSON mode could not make JSON) goes to the repair attempt with what was written."""

    provider = "openai_compatible"

    def __init__(self, base_url: str, api_key: str, models: Mapping[str, str], **kw):
        if not base_url:
            raise ProviderError("OPENAI_BASE_URL is not set")
        if not api_key:
            raise ProviderError("OPENAI_API_KEY is not set")
        super().__init__(models, **kw)
        self.url = base_url.rstrip("/") + "/chat/completions"
        self._key = api_key
        self.json_mode, self.tokens_field, self.send_temperature = True, "max_tokens", True

    def __repr__(self) -> str:
        return f"OpenAICompatibleClient({self.url!r}, {self.models['large']!r})"  # never the key

    def describe(self) -> str:
        host = re.sub(r"^https?://([^/]+).*$", r"\1", self.url)
        return f"{host} · {self.models['large']}"

    def _body(self, model, system, user, temperature, max_tokens, images) -> dict:
        content: str | list = user
        if images:
            content = [{"type": "text", "text": user}] + [
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + _b64(png)}} for png in images
            ]
        body = {
            "model": model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}],
            self.tokens_field: max(max_tokens, MIN_TOKENS_OPENAI),
        }
        if self.send_temperature:
            body["temperature"] = temperature
        if self.json_mode:
            body["response_format"] = {"type": "json_object"}
        return body

    def _send(self, model, system, user, temperature, max_tokens, images):
        headers = {"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"}
        for _ in range(4):  # at most one retry per refused parameter
            body = self._body(model, system, user, temperature, max_tokens, images)
            try:
                out = self._post(self.url, headers, body)
                break
            except RequestRefused as e:
                if e.status != 400:
                    raise
                failed = _failed_generation(e.body)
                if failed is not None:  # JSON mode gave up: let the repair attempt see what was written
                    return failed, None, None
                if not self._drop_refused(e.body, body):
                    raise
        else:
            raise ProviderError(f"{self.provider}: the server kept refusing the request")
        choice = (out.get("choices") or [{}])[0] or {}
        if choice.get("finish_reason") == "length":
            log.warning("openai_compatible reply cut at the token limit (%s)", model)
        content = (choice.get("message") or {}).get("content") or ""
        if isinstance(content, list):  # some servers send content parts
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        usage = out.get("usage") or {}
        return content, usage.get("prompt_tokens"), usage.get("completion_tokens")

    def _drop_refused(self, error_text: str, sent: dict) -> bool:
        """Adapt to a 400 that names a parameter of the request just sent. True when the next request differs.
        Decided from `sent`, so two calls refused at once both retry (the flags only remember it for later calls)."""
        low = error_text.lower()
        if "response_format" in sent and ("response_format" in low or "json_object" in low or "json mode" in low):
            self.json_mode = False
            return True
        if "max_tokens" in sent and "max_tokens" in low and "max_completion_tokens" in low:
            self.tokens_field = "max_completion_tokens"
            return True
        if "temperature" in sent and "temperature" in low:
            self.send_temperature = False
            return True
        return False


def _failed_generation(error_text: str) -> str | None:
    """What the model wrote when Groq's JSON mode refused it (error code json_validate_failed), else None."""
    if "json_validate_failed" not in (error_text or ""):
        return None
    try:
        err = json.loads(error_text).get("error") or {}
    except (json.JSONDecodeError, AttributeError):
        return ""
    return str(err.get("failed_generation") or "")


def from_env(env: Mapping[str, str] | None = None, *, post: Post | None = None) -> JsonClient | None:
    """The client Section 11.1 configures, or None when no LLM key is set (module 6 then needs ready scene plans).

    LLM_PROVIDER        anthropic (default) | openai_compatible
    ANTHROPIC_API_KEY   for anthropic
    OPENAI_BASE_URL, OPENAI_API_KEY   for openai_compatible
    LLM_MODEL_LARGE, LLM_MODEL_SMALL, LLM_MODEL_VISION   (openai_compatible: LARGE is required, SMALL follows it,
                         VISION only when set; without it custom drawings are not judged and fall back)
    LLM_MAX_CONCURRENCY  default 8"""
    env = os.environ if env is None else env
    provider = (env.get("LLM_PROVIDER") or "anthropic").strip().lower()
    try:
        concurrency = int(env.get("LLM_MAX_CONCURRENCY") or 8)
    except ValueError:
        raise ProviderError("LLM_MAX_CONCURRENCY must be a whole number") from None
    pick = {role: (env.get(f"LLM_MODEL_{role.upper()}") or "").strip() for role in DEFAULT_MODELS}
    if provider == "anthropic":
        key = (env.get("ANTHROPIC_API_KEY") or "").strip()
        if not key:
            return None
        models = {role: pick[role] or DEFAULT_MODELS[role] for role in DEFAULT_MODELS}
        return AnthropicClient(key, models, max_concurrency=concurrency, post=post)
    if provider == "openai_compatible":
        key = (env.get("OPENAI_API_KEY") or "").strip()
        if not key:
            return None
        if not pick["large"]:
            raise ProviderError("LLM_MODEL_LARGE must name a model of that provider (for example openai/gpt-oss-120b)")
        # a vision model only when named: the large one may not see images (then drawings are not judged)
        models = {"large": pick["large"], "small": pick["small"] or pick["large"], "vision": pick["vision"]}
        return OpenAICompatibleClient(
            (env.get("OPENAI_BASE_URL") or "").strip(), key, models, max_concurrency=concurrency, post=post
        )
    raise ProviderError(f"LLM_PROVIDER must be anthropic or openai_compatible, not {provider!r}")
