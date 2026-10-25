"""S6 Audio Producer (TTS + STT + ffmpeg) — Section 9.

For every line: synthesize `text_spoken` with the line's voice, transcode to MP3 (mono, 24 kHz, CBR 96 kbps, silence
trimmed, −16 LUFS), transcribe it back with STT, align the recognized words to the DISPLAY tokens (so captions can
highlight the word being said, digits included: the display token "1/4" spans the spoken "واحد من أربعة"), and check it.
Output `audio.json`: per line `{line_id, file, duration_ms, voice_id, words: [{w, s, e}], timing_source}`.

No code here knows a provider's specifics: voices come from `config/voices.yaml`, the providers from the registry."""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

import yaml

from ...core.retry import ProviderError
from ...validators.text import norm_ar, strip_emoji, tokens, word_error_rate
from .. import audio_tools as at
from ..models import AudioLine, AudioOut, Cast, Intake, Script, ValidationReport, Warning_, Word

VOICES_FILE = Path(__file__).resolve().parents[4] / "config" / "voices.yaml"
SPLIT_GAP_MS, MIN_MS, MAX_MS = 120, 500, 25_000
WER_OK, WER_MINOR, RESYNTH_RETRIES = 0.25, 0.35, 2
RATE_MIN, RATE_MAX, SILENT_DB = 1.2, 4.0, -35.0
AGE_RELAX = {
    "child": ["child", "teen", "adult"],
    "teen": ["teen", "adult"],
    "adult": ["adult"],
    "elder": ["elder", "adult"],
}


@dataclass(frozen=True)
class Voice:
    id: str
    provider: str
    voice_id: str
    gender: str
    age_band: str
    dialects: tuple[str, ...]
    languages: tuple[str, ...]


def load_voices(path: Path | None = None, provider: str | None = None) -> list[Voice]:
    raw = yaml.safe_load(Path(path or VOICES_FILE).read_text(encoding="utf-8"))["voices"]
    voices = [
        Voice(
            id=v["id"],
            provider=v["provider"],
            voice_id=v["voice_id"],
            gender=v["gender"],
            age_band=v["age_band"],
            dialects=tuple(v.get("dialects") or ()),
            languages=tuple(v["languages"]),
        )
        for v in raw
    ]
    return [v for v in voices if provider is None or v.provider == provider]


# ----------------------------------------------------------------------------------------------- voices (9.2)
@dataclass
class Assigned:
    voice: Voice
    pitch: int = 0  # semitones (only when voices ran out)


def assign_voices(
    script: Script,
    cast: Cast,
    language: str,
    dialect: str | None,
    voices: list[Voice],
    overrides: dict | None = None,
    narrator_gender: str = "f",
) -> dict[str, Assigned]:
    usable = [v for v in voices if language in v.languages]
    if not usable:
        raise ProviderError(f"no voice for language {language!r}")
    dialect = dialect or "msa"

    def fits_dialect(v, d):
        return language != "ar" or d in v.dialects

    out: dict[str, Assigned] = {}
    used: dict[str, int] = {}
    # narrator: the override, else the first adult voice of NARRATOR_GENDER (dialect_applied, else msa)
    nar = (overrides or {}).get("narrator")
    if nar:
        v = next((x for x in voices if x.id == nar or x.voice_id == nar), None) or Voice(
            id=nar,
            provider="override",
            voice_id=nar,
            gender=narrator_gender,
            age_band="adult",
            dialects=(dialect,),
            languages=(language,),
        )
    else:
        v = (
            next(
                (
                    x
                    for d in (dialect, "msa")
                    for x in usable
                    if x.gender == narrator_gender and x.age_band == "adult" and fits_dialect(x, d)
                ),
                None,
            )
            or usable[0]
        )
    out["narrator"] = Assigned(v)
    used[v.id] = 1
    # characters in order of first appearance
    order = []
    for line in script.all_lines():
        if line.speaker != "narrator" and line.speaker not in order:
            order.append(line.speaker)
    shift = 2
    for key in order:
        cc = cast.character(key)
        gender = cc.presentation if cc else (script.character(key).gender if script.character(key) else "f")
        band = cc.voice_profile.age_band if cc else "adult"
        pick = None
        for d in (dialect, "msa"):
            for b in AGE_RELAX.get(band, [band, "adult"]):
                cands = [x for x in usable if x.gender == gender and x.age_band == b and fits_dialect(x, d)]
                fresh = [x for x in cands if x.id not in used]
                if fresh:
                    pick = fresh[0]
                    break
            if pick:
                break
        if pick:
            out[key] = Assigned(pick)
            used[pick.id] = used.get(pick.id, 0) + 1
            continue
        # voices ran out: reuse the least-used voice of that gender (else any) and shift its pitch ±2 semitones
        pool = [x for x in usable if x.gender == gender] or usable
        reuse = min(pool, key=lambda x: (used.get(x.id, 0), pool.index(x)))
        out[key] = Assigned(reuse, pitch=shift)
        used[reuse.id] = used.get(reuse.id, 0) + 1
        shift = -shift
    return out


# ----------------------------------------------------------------------------------------------- text (9.3)
_SENTENCE_END = re.compile(r"(?<=[.!?؟؛،,;])\s+")


def split_text(text: str, max_chars: int) -> list[str]:
    """Split at sentence boundaries so every part fits the provider's limit (then at spaces if a sentence is long)."""
    text = strip_emoji(text).strip()
    if len(text) <= max_chars:
        return [text]
    parts, cur = [], ""
    for sent in _SENTENCE_END.split(text):
        for piece in [sent] if len(sent) <= max_chars else _by_words(sent, max_chars):
            if cur and len(cur) + 1 + len(piece) > max_chars:
                parts.append(cur)
                cur = piece
            else:
                cur = f"{cur} {piece}".strip()
    if cur:
        parts.append(cur)
    return parts


def _by_words(s: str, max_chars: int) -> list[str]:
    out, cur = [], ""
    for w in s.split():
        if cur and len(cur) + 1 + len(w) > max_chars:
            out.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    return out + ([cur] if cur else [])


# ----------------------------------------------------------------------------------------------- word timings (9.5)
def _spread(n_chars: list[int], start: float, end: float) -> list[tuple[float, float]]:
    total = sum(max(1, c) for c in n_chars) or 1
    out, t = [], start
    for c in n_chars:
        d = (end - start) * max(1, c) / total
        out.append((t, t + d))
        t += d
    return out


def align_words(display: str, spoken: str, asr_words: list[dict], duration_ms: int) -> tuple[list[Word], str]:
    """One entry per display token with times inside the line. `stt` when ASR matched at least 85% of the spoken
    tokens, else `estimated` (the line spread over the tokens by character count)."""
    d_tok, s_tok = tokens(display), tokens(spoken)
    if not d_tok:
        return [], "estimated"
    s_norm = [norm_ar(t) for t in s_tok]
    a_norm = [norm_ar(w["w"]) for w in asr_words]
    s_times: list[tuple[float, float] | None] = [None] * len(s_tok)
    if s_tok and a_norm:
        for blk in SequenceMatcher(None, a_norm, s_norm, autojunk=False).get_matching_blocks():
            for k in range(blk.size):
                w = asr_words[blk.a + k]
                s_times[blk.b + k] = (float(w["s"]), float(w["e"]))
    matched = sum(t is not None for t in s_times) / max(1, len(s_tok))
    if matched < 0.85:
        spans = _spread([len(norm_ar(t)) for t in d_tok], 0, duration_ms)
        return _finish(d_tok, spans, duration_ms), "estimated"
    # fill the spoken tokens ASR did not hear, between their known neighbours
    i = 0
    while i < len(s_times):
        if s_times[i] is not None:
            i += 1
            continue
        j = i
        while j < len(s_times) and s_times[j] is None:
            j += 1
        lo = s_times[i - 1][1] if i > 0 else 0.0
        hi = s_times[j][0] if j < len(s_times) else float(duration_ms)
        for k, span in enumerate(_spread([len(s_norm[x]) for x in range(i, j)], lo, max(lo, hi)), start=i):
            s_times[k] = span
        i = j
    # display tokens <- spoken tokens (digits: one display token may span several spoken words)
    d_norm = [norm_ar(t) for t in d_tok]
    spans: list[tuple[float, float] | None] = [None] * len(d_tok)
    for tag, i1, i2, j1, j2 in SequenceMatcher(None, d_norm, s_norm, autojunk=False).get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                spans[i1 + k] = s_times[j1 + k]
        elif tag == "replace":
            lo, hi = s_times[j1][0], s_times[j2 - 1][1]
            for k, sp in enumerate(_spread([len(d_norm[x]) for x in range(i1, i2)], lo, hi), start=i1):
                spans[k] = sp
    # display tokens with nothing spoken (a dash, an emoji…) get a short span where they sit
    for k in range(len(spans)):
        if spans[k] is None:
            prev = spans[k - 1][1] if k > 0 and spans[k - 1] else 0.0
            spans[k] = (prev, prev)
    return _finish(d_tok, spans, duration_ms), "stt"


def _finish(d_tok, spans, duration_ms) -> list[Word]:
    out, last_e = [], 0
    for tok, (s, e) in zip(d_tok, spans):
        s_i = max(int(round(s)), last_e if last_e < duration_ms else duration_ms - 1)
        s_i = min(s_i, duration_ms - 1)
        e_i = min(max(int(round(e)), s_i + 1), duration_ms)
        out.append(Word(w=tok, s=s_i, e=e_i))
        last_e = e_i
    return out


# ----------------------------------------------------------------------------------------------- one line
def _synthesize(tts, text: str, assigned: Assigned, language: str, dialect: str | None) -> bytes:
    parts = split_text(text, tts.max_chars)
    audio = [tts.synthesize(p, assigned.voice.voice_id, language, dialect).bytes for p in parts]
    raw = audio[0] if len(audio) == 1 else at.join_with_silence(audio, SPLIT_GAP_MS)
    return at.to_mp3(raw, pitch_semitones=assigned.pitch)


def _failed_line(line, assigned: Assigned, rep: ValidationReport, code: str, message: str, suspect: str):
    """A line that produced no usable audio: one blocker, and an empty entry so the other lines still finish."""
    rep.add(
        f"audio_{line.id}", False, "blocker", f"{line.id}: {message}", code=code, line_id=line.id, suspect_stage=suspect
    )
    return AudioLine(
        line_id=line.id,
        file=f"audio/{line.id}.mp3",
        duration_ms=0,
        voice_id=assigned.voice.id,
        words=[],
        timing_source="estimated",
    ), b""


def produce_line(
    line, assigned: Assigned, *, tts, stt, language: str, dialect: str | None, rep: ValidationReport
) -> tuple[AudioLine, bytes]:
    spoken = strip_emoji(line.text_spoken)
    if not tokens(spoken):
        return _failed_line(line, assigned, rep, "AUDIO_EMPTY_TEXT", "text_spoken has no words to speak", "S4_writer")
    best = None
    try:
        for _ in range(1 + RESYNTH_RETRIES):
            mp3 = _synthesize(tts, spoken, assigned, language, dialect)
            heard = stt.transcribe(mp3, language, reference_text=spoken)
            wer = word_error_rate(spoken, heard.get("text", ""))
            if best is None or wer < best[0]:
                best = (wer, mp3, heard)
            if wer <= WER_OK:
                break
        wer, mp3, heard = best
        info = at.probe(mp3)
    except at.AudioToolError as e:
        return _failed_line(line, assigned, rep, "AUDIO_DECODE", f"the audio could not be processed ({e})", "S6_audio")
    dur = info["duration_ms"]
    words, source = align_words(line.text_display, spoken, heard.get("words") or [], max(dur, 1))
    ok_decode = info["codec"] == "mp3" and info["channels"] == 1 and info["sample_rate"] == 24000
    lid = line.id
    rep.add(
        f"decodes_{lid}",
        ok_decode,
        "blocker",
        f"{lid}: {info}",
        code="AUDIO_DECODE",
        line_id=lid,
        suspect_stage="S6_audio",
    )
    rep.add(
        f"duration_{lid}",
        MIN_MS <= dur <= MAX_MS,
        "blocker",
        f"{lid} lasts {dur} ms (0.5 to 25 s)",
        code="AUDIO_DURATION",
        line_id=lid,
        suspect_stage="S4_writer",
        fix_hint="split or shorten the line",
    )
    vol = at.mean_volume_db(mp3)
    rep.add(
        f"not_silent_{lid}",
        vol > SILENT_DB,
        "blocker",
        f"{lid} mean volume {vol:.1f} dB",
        code="AUDIO_SILENT",
        line_id=lid,
        suspect_stage="S6_audio",
    )
    n_words = len(tokens(spoken))
    rate = n_words / (dur / 1000) if dur else 0
    rep.add(
        f"speech_rate_{lid}",
        RATE_MIN <= rate <= RATE_MAX,
        "major",
        f"{lid}: {rate:.2f} words per second",
        code="SPEECH_RATE",
        line_id=lid,
        suspect_stage="S6_audio",
    )
    if wer <= WER_OK:
        rep.add(f"round_trip_{lid}", True, "minor")
    elif wer <= WER_MINOR:
        rep.add(
            f"round_trip_{lid}", False, "minor", f"{lid}: word error rate {wer:.2f}", code="AUDIO_MISMATCH", line_id=lid
        )
    else:
        rep.add(
            f"round_trip_{lid}",
            False,
            "major",
            f"{lid}: word error rate {wer:.2f}",
            code="AUDIO_MISMATCH",
            line_id=lid,
            suspect_stage="S6_audio",
        )
    return AudioLine(
        line_id=lid,
        file=f"audio/{lid}.mp3",
        duration_ms=dur,
        voice_id=assigned.voice.id,
        words=words,
        timing_source=source,
    ), mp3


# ----------------------------------------------------------------------------------------------- the stage
def run(
    script: Script,
    cast: Cast,
    intake: Intake,
    *,
    voices: list[Voice],
    tts,
    stt,
    blob=None,
    voice_overrides: dict | None = None,
    narrator_gender: str = "f",
    concurrency: int = 4,
    only_lines: set[str] | None = None,
    previous: AudioOut | None = None,
) -> AudioOut:
    """Produce every line (or only `only_lines`, reusing `previous` for the rest: revisions). Raises ProviderError
    when no voice fits the language. The report is in `out._report`, the MP3 bytes in `out._files`."""
    usable = [v for v in voices if v.provider in (tts.name, "override")] or voices
    assigned = assign_voices(
        script, cast, intake.language, intake.dialect_applied, usable, voice_overrides, narrator_gender
    )
    rep = ValidationReport(stage="S6")
    todo = [ln for ln in script.all_lines() if only_lines is None or ln.id in only_lines]

    def work(line):
        return produce_line(
            line,
            assigned[line.speaker],
            tts=tts,
            stt=stt,
            language=intake.language,
            dialect=intake.dialect_applied,
            rep=rep,
        )

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        results = dict(zip([ln.id for ln in todo], pool.map(work, todo)))
    lines, files = [], {}
    for ln in script.all_lines():
        if ln.id in results:
            al, mp3 = results[ln.id]
            files[al.file] = mp3
            if blob is not None:
                blob.put(al.file, mp3)
        else:
            al = previous.line(ln.id) if previous else None
            if al is None:
                raise ValueError(f"no audio for line {ln.id}")
        lines.append(al)
    warnings = [
        Warning_(code="VOICE_REUSED", message=f"{k} reuses {a.voice.id} shifted {a.pitch:+d} semitones")
        for k, a in assigned.items()
        if a.pitch
    ]
    out = AudioOut(voices={k: a.voice.id for k, a in assigned.items()}, lines=lines, warnings=warnings)
    out._report, out._files = rep, files
    return out


def measured_wps(script: Script, audio: AudioOut) -> float:
    """Spoken words per second over the whole story (9.7): the value to put in SPEECH_WPS_AR / SPEECH_WPS_EN once
    real voices have produced a few stories. Words are counted on `text_spoken`, as the speech-rate check does."""
    words = seconds = 0.0
    for ln in script.all_lines():
        al = audio.line(ln.id)
        if al is not None and al.duration_ms > 0:
            words += len(tokens(ln.text_spoken))
            seconds += al.duration_ms / 1000
    return round(words / seconds, 2) if seconds else 0.0
