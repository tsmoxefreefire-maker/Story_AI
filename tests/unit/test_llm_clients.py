"""The real LLM clients (providers/llm/clients.py) with a fake HTTP transport: the request each provider gets, the JSON
rule and the one repair attempt, retries, refused parameters, usage, and that the key never shows."""

import json

import pytest

from story_service.core.retry import ProviderError
from story_service.providers.llm.clients import (
    AnthropicClient,
    OpenAICompatibleClient,
    from_env,
    parse_json,
)

KEY = "secret-key-123"
SCHEMA = {
    "type": "object",
    "properties": {"scene_id": {"type": "string"}, "n": {"type": "integer"}},
    "required": ["scene_id"],
}


class FakePost:
    """Answers in order: each answer is (status, text). Records every request."""

    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    def __call__(self, url, headers, body, timeout):
        self.calls.append({"url": url, "headers": headers, "body": json.loads(json.dumps(body))})
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]


def openai_reply(content, tokens=(10, 5)):
    return 200, json.dumps(
        {
            "choices": [{"message": {"content": content}}],
            "usage": dict(zip(("prompt_tokens", "completion_tokens"), tokens)),
        }
    )


def anthropic_reply(text):
    return 200, json.dumps(
        {"content": [{"type": "text", "text": text}], "usage": {"input_tokens": 7, "output_tokens": 3}}
    )


def oa(post, **kw):
    c = OpenAICompatibleClient(
        "https://api.groq.com/openai/v1/",
        KEY,
        {"large": "m-large", "small": "m-small", "vision": "m-vis"},
        post=post,
        **kw,
    )
    c.sleep = lambda s: None
    return c


# ----------------------------------------------------------------------------------------------- from_env
def test_no_key_means_no_llm():
    assert from_env({}) is None
    assert from_env({"LLM_PROVIDER": "openai_compatible", "OPENAI_BASE_URL": "https://x"}) is None


def test_from_env_builds_each_provider_without_showing_the_key():
    c = from_env({"ANTHROPIC_API_KEY": KEY})
    assert isinstance(c, AnthropicClient) and c.models["large"] == "claude-sonnet-5-5" and KEY not in repr(c)
    env = {
        "LLM_PROVIDER": "openai_compatible",
        "OPENAI_BASE_URL": "https://api.groq.com/openai/v1",
        "OPENAI_API_KEY": KEY,
    }
    with pytest.raises(ProviderError, match="LLM_MODEL_LARGE"):
        from_env(env)
    c = from_env(env | {"LLM_MODEL_LARGE": "openai/gpt-oss-120b"})
    assert isinstance(c, OpenAICompatibleClient)
    assert c.models == {"large": "openai/gpt-oss-120b", "small": "openai/gpt-oss-120b", "vision": ""}
    assert c.describe() == "api.groq.com · openai/gpt-oss-120b" and KEY not in repr(c)
    with pytest.raises(ProviderError, match="LLM_PROVIDER"):
        from_env({"LLM_PROVIDER": "magic", "OPENAI_API_KEY": KEY})


# ----------------------------------------------------------------------------------------------- requests
def test_openai_compatible_request_and_usage():
    post = FakePost(openai_reply('```json\n{"scene_id": "s01", "n": 2}\n```'))
    c = oa(post)
    out = c.complete_json("You direct scenes.", "the scene", SCHEMA, "vision", 0.4, 2000, images=[b"\x89PNG"])
    assert out == {"scene_id": "s01", "n": 2}
    req = post.calls[0]
    assert req["url"] == "https://api.groq.com/openai/v1/chat/completions"
    assert req["headers"]["Authorization"] == f"Bearer {KEY}"
    body = req["body"]
    assert body["model"] == "m-vis" and body["max_tokens"] == 2000 and body["temperature"] == 0.4
    assert body["response_format"] == {"type": "json_object"}
    assert "JSON Schema" in body["messages"][0]["content"] and '"scene_id"' in body["messages"][0]["content"]
    parts = body["messages"][1]["content"]
    assert parts[0] == {"type": "text", "text": "the scene"} and parts[1]["image_url"]["url"].startswith(
        "data:image/png;base64,"
    )
    assert c.usage[0] | {"latency_ms": 0} == {
        "provider": "openai_compatible",
        "model": "m-vis",
        "role": "vision",
        "tokens_in": 10,
        "tokens_out": 5,
        "latency_ms": 0,
    }


def test_anthropic_request():
    post = FakePost(anthropic_reply('{"scene_id": "s02"}'))
    c = AnthropicClient(KEY, {"large": "claude-sonnet-5-5", "small": "h", "vision": "v"}, post=post)
    assert c.complete_json("sys", "user", SCHEMA, "large", 0.2, 500, images=[b"img"]) == {"scene_id": "s02"}
    req = post.calls[0]
    assert req["url"] == "https://api.anthropic.com/v1/messages"
    assert req["headers"]["x-api-key"] == KEY and req["headers"]["anthropic-version"] == "2023-06-01"
    assert req["body"]["system"].startswith("sys") and req["body"]["max_tokens"] == 500
    assert req["body"]["messages"][0]["content"][1]["source"]["media_type"] == "image/png"
    assert c.usage[0]["tokens_in"] == 7


# ----------------------------------------------------------------------------------------------- JSON rule
def test_one_repair_attempt_then_the_answer():
    post = FakePost(openai_reply("Sure! Here is the plan."), openai_reply('{"scene_id": "s03"}'))
    assert oa(post).complete_json("s", "u", SCHEMA, "large", 0.3, 100) == {"scene_id": "s03"}
    assert "could not be used" in post.calls[1]["body"]["messages"][1]["content"]


def test_no_json_after_the_repair_gives_an_empty_answer_for_the_stage_to_refuse():
    # S7 then counts a failed attempt and asks again with feedback; S8 falls back to the generic prop
    post = FakePost(openai_reply("no"), openai_reply("still no"))
    assert oa(post).complete_json("s", "u", SCHEMA, "large", 0.3, 100) == {}


def test_groq_json_validate_failed_goes_to_the_repair_attempt():
    error = {
        "error": {"message": "Failed to generate JSON", "code": "json_validate_failed", "failed_generation": "{bad"}
    }
    post = FakePost((400, json.dumps(error)), openai_reply('{"scene_id": "s04"}'))
    assert oa(post).complete_json("s", "u", SCHEMA, "large", 0.3, 100) == {"scene_id": "s04"}
    assert "{bad" in post.calls[1]["body"]["messages"][1]["content"]


def test_images_without_a_vision_model_are_not_judged():
    post = FakePost(openai_reply('{"scene_id": "x"}'))
    c = OpenAICompatibleClient(
        "https://x/v1", KEY, {"large": "text-only", "small": "text-only", "vision": ""}, post=post
    )
    assert c.complete_json("s", "u", SCHEMA, "vision", 0.2, 200, images=[b"png"]) == {} and post.calls == []
    refused = FakePost((400, json.dumps({"error": {"message": "this model does not support image input"}})))
    assert oa(refused).complete_json("s", "u", SCHEMA, "vision", 0.2, 200, images=[b"png"]) == {}


def test_content_parts_and_reasoning_budget():
    reply = 200, json.dumps({"choices": [{"message": {"content": [{"type": "text", "text": '{"scene_id": "p"}'}]}}]})
    post = FakePost(reply)
    assert oa(post).complete_json("s", "u", SCHEMA, "vision", 0.2, 200, images=[b"png"]) == {"scene_id": "p"}
    assert post.calls[0]["body"]["max_tokens"] == 1024  # room for a reasoning model to answer


def test_json_off_the_schema_is_repaired_once_then_left_to_the_stage():
    post = FakePost(openai_reply('{"n": "two"}'), openai_reply('{"n": 3}'))
    assert oa(post).complete_json("s", "u", SCHEMA, "large", 0.3, 100) == {"n": 3}  # stage checks will refuse it
    assert "does not match the schema" in post.calls[1]["body"]["messages"][1]["content"]


@pytest.mark.parametrize(
    "text,ok",
    [
        ('{"scene_id": "a"}', True),
        ('text {"scene_id": "a"} more', True),
        ("[1, 2]", False),
        ("", False),
        ('{"n": 1}', False),
    ],
)
def test_parse_json(text, ok):
    data, problem = parse_json(text, SCHEMA)
    assert (problem is None) == ok


# ----------------------------------------------------------------------------------------------- errors
def test_transient_errors_are_retried_and_others_are_provider_errors():
    post = FakePost((429, "slow down", 7.0), (503, "busy"), openai_reply('{"scene_id": "s"}'))
    c = oa(post)
    waits = []
    c.sleep = waits.append
    assert c.complete_json("s", "u", SCHEMA, "large", 0.3, 100) == {"scene_id": "s"} and len(post.calls) == 3
    assert waits[0] == 7.0  # the provider's Retry-After
    with pytest.raises(ProviderError) as e:
        oa(FakePost((401, '{"error": "invalid api key"}'))).complete_json("s", "u", SCHEMA, "large", 0.3, 100)
    assert "401" in str(e.value) and KEY not in str(e.value)


def err(message):
    return json.dumps({"error": {"message": message, "type": "invalid_request_error"}})


def test_refused_parameters_are_dropped_once_each():
    post = FakePost(
        (400, err("response_format json_object is not supported")),
        (
            400,
            err("Unsupported parameter: 'max_tokens' is not supported. Use 'max_completion_tokens' instead."),
        ),
        (400, err("Unsupported value: 'temperature' does not support 0.3")),
        openai_reply('{"scene_id": "s"}'),
    )
    c = oa(post)
    assert c.complete_json("s", "u", SCHEMA, "large", 0.3, 100) == {"scene_id": "s"}
    last = post.calls[-1]["body"]
    assert "response_format" not in last and "temperature" not in last and last["max_completion_tokens"] == 1024
    with pytest.raises(ProviderError):
        oa(FakePost((400, err("model not found")))).complete_json("s", "u", SCHEMA, "large", 0.3, 100)


def test_a_parameter_another_call_already_dropped_is_still_retried():
    c = oa(FakePost(openai_reply("{}")))
    c.json_mode = False  # another thread dropped JSON mode a moment ago
    assert c._drop_refused("response_format is not supported", {"response_format": {"type": "json_object"}})
    assert not c._drop_refused("response_format is not supported", {"model": "m"})
