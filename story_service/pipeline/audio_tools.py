"""ffmpeg and ffprobe helpers for S6 (Section 9.4). Every call pipes bytes in and out: no temp files, no shell."""

from __future__ import annotations

import io
import json
import re
import subprocess
import wave

FFMPEG, FFPROBE = "ffmpeg", "ffprobe"
RATE = 24000


class AudioToolError(RuntimeError):
    pass


def _run(args: list[str], data: bytes, timeout: float = 120) -> subprocess.CompletedProcess:
    p = subprocess.run(args, input=data, capture_output=True, timeout=timeout)
    if p.returncode != 0:
        raise AudioToolError(p.stderr.decode("utf-8", "replace")[-400:])
    return p


def probe(audio: bytes) -> dict:
    """`{codec, channels, sample_rate, duration_ms}` of the first audio stream."""
    p = _run([FFPROBE, "-v", "error", "-print_format", "json", "-show_streams", "-show_format", "pipe:0"], audio)
    info = json.loads(p.stdout or b"{}")
    st = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), None)
    if not st:
        raise AudioToolError("no audio stream")
    dur = float(st.get("duration") or info.get("format", {}).get("duration") or 0)
    if not dur:  # MP3 from a pipe may need a full decode to know its length
        dur = len(decode_pcm(audio)) / 2 / RATE
    return {
        "codec": st.get("codec_name"),
        "channels": int(st.get("channels", 0)),
        "sample_rate": int(st.get("sample_rate", 0)),
        "duration_ms": round(dur * 1000),
    }


def decode_pcm(audio: bytes) -> bytes:
    """Decode anything to raw 16-bit mono PCM at 24 kHz."""
    return _run(
        [FFMPEG, "-v", "error", "-i", "pipe:0", "-ac", "1", "-ar", str(RATE), "-f", "s16le", "pipe:1"], audio
    ).stdout


def to_wav(audio: bytes) -> bytes:
    pcm = decode_pcm(audio)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm)
    return buf.getvalue()


def join_with_silence(parts: list[bytes], gap_ms: int = 120) -> bytes:
    """Join audio parts (any format) with `gap_ms` of silence between them; returns WAV (used for long lines, 9.3)."""
    silence = b"\x00\x00" * int(RATE * gap_ms / 1000)
    pcm = silence.join(decode_pcm(p) for p in parts)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm)
    return buf.getvalue()


def _pitch_filter(semitones: int) -> str:
    """Shift pitch by `semitones` while keeping the duration (asetrate with atempo compensation, Section 9.2)."""
    if not semitones:
        return ""
    k = 2 ** (semitones / 12)
    return f"asetrate={RATE * k:.0f},aresample={RATE},atempo={1 / k:.6f},"


def to_mp3(audio: bytes, pitch_semitones: int = 0) -> bytes:
    """Mono, 24 kHz; trim silence below −45 dB at both ends keeping 80 ms; loudness −16 LUFS (true peak −1.5 dBTP);
    MP3 libmp3lame CBR 96 kbps."""
    trim = "silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.08"
    head = f"aformat=channel_layouts=mono,aresample={RATE},{_pitch_filter(pitch_semitones)}"
    tail = f"loudnorm=I=-16:TP=-1.5:LRA=11,aresample={RATE}"

    def encode(chain: str) -> bytes:
        return _run(
            [
                FFMPEG,
                "-v",
                "error",
                "-i",
                "pipe:0",
                "-af",
                chain,
                "-ac",
                "1",
                "-ar",
                str(RATE),
                "-c:a",
                "libmp3lame",
                "-b:a",
                "96k",
                "-f",
                "mp3",
                "pipe:1",
            ],
            audio,
        ).stdout

    out = encode(f"{head}{trim},areverse,{trim},areverse,{tail}")
    if len(out) < 1000:  # all of it was "silence": keep it as is (no trim, and loudnorm cannot measure
        out = encode(f"{head}aresample={RATE}")  # silence) so the not-silent check of S6 is the one that judges it
    return out


def mean_volume_db(audio: bytes) -> float:
    p = _run([FFMPEG, "-v", "info", "-i", "pipe:0", "-af", "volumedetect", "-f", "null", "-"], audio)
    m = re.search(rb"mean_volume:\s*(-?[\d.]+|-inf) dB", p.stderr)
    if not m or m.group(1) == b"-inf":
        return -120.0
    return float(m.group(1))


def mp3_bitrate_kbps(audio: bytes) -> int:
    p = _run([FFPROBE, "-v", "error", "-print_format", "json", "-show_streams", "pipe:0"], audio)
    st = json.loads(p.stdout)["streams"][0]
    return round(int(st.get("bit_rate", 0)) / 1000)
