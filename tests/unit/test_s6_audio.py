"""S6 Audio Producer (spec table) with FakeTTS / FakeSTT and the real ffmpeg."""

import shutil

import pytest

import golden
from story_service.core.retry import ProviderError
from story_service.pipeline import audio_tools
from story_service.pipeline.models import Cast, Line, Script, ValidationReport
from story_service.pipeline.stages import s6_audio
from story_service.pipeline.stages.s6_audio import Assigned, Voice, align_words, assign_voices, load_voices, split_text
from story_service.providers.blobstore import MemoryBlobStore
from story_service.providers.stt import FakeSTT
from story_service.providers.tts import FakeTTS

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")
VOICE = Voice(
    id="fake_ar_f_adult_1",
    provider="fake",
    voice_id="f1",
    gender="f",
    age_band="adult",
    dialects=("msa",),
    languages=("ar",),
)


def one_line(text, display=None, lid="s01_l01"):
    return Line(id=lid, speaker="narrator", text_display=display or text, text_spoken=text)


def produce(line, tts=None, stt=None):
    rep = ValidationReport(stage="S6")
    al, mp3 = s6_audio.produce_line(
        line, Assigned(VOICE), tts=tts or FakeTTS(), stt=stt or FakeSTT(), language="ar", dialect="msa", rep=rep
    )
    return al, mp3, rep


@needs_ffmpeg
def test_s6_golden():
    blob = MemoryBlobStore()
    out = s6_audio.run(
        golden.script(),
        golden.cast(),
        golden.intake(),
        voices=load_voices(provider="fake"),
        tts=FakeTTS(),
        stt=FakeSTT(),
        blob=blob,
    )
    l1 = out.lines[0]
    assert abs(l1.duration_ms - 4400) <= 100 and l1.timing_source == "stt"
    assert len({out.voices["nadia"], out.voices["karim"], out.voices["narrator"]}) == 3
    info = audio_tools.probe(blob.get(l1.file))
    assert info["channels"] == 1 and info["sample_rate"] == 24000 and info["codec"] == "mp3"
    for line in out.lines:
        for w in line.words:
            assert 0 <= w.s < w.e <= line.duration_ms
    assert out._report.passed and len(out.lines) == len(golden.script().all_lines())


def test_no_voice_for_language():
    english_only = [v for v in load_voices() if "ar" not in v.languages]
    with pytest.raises(ProviderError):
        assign_voices(golden.script(), golden.cast(), "ar", "msa", english_only)


def test_voices_run_out_reuse_with_pitch_shift():
    data = golden.script().model_dump()
    data["characters"] = [dict(data["characters"][0], key=f"g{i}") for i in range(5)]
    data["scenes"] = [
        dict(
            data["scenes"][0],
            lines=[dict(data["scenes"][0]["lines"][0], id=f"s01_l0{i + 1}", speaker=f"g{i}") for i in range(5)],
        )
    ]
    script = Script.model_validate(data)
    cast_data = golden.cast().model_dump()
    cast_data["characters"] = [dict(cast_data["characters"][0], key=f"g{i}") for i in range(5)]
    cast = Cast.model_validate(cast_data)
    three = [
        Voice(
            id=f"v{i}",
            provider="fake",
            voice_id=f"v{i}",
            gender="f",
            age_band="adult",
            dialects=("msa",),
            languages=("ar",),
        )
        for i in range(3)
    ]
    got = assign_voices(script, cast, "ar", "msa", three)
    pitches = sorted(a.pitch for a in got.values() if a.pitch)
    assert pitches and set(pitches) <= {2, -2} and 2 in pitches


def test_narrator_override():
    got = assign_voices(
        golden.script(), golden.cast(), "ar", "msa", load_voices(provider="fake"), overrides={"narrator": "x"}
    )
    assert got["narrator"].voice.voice_id == "x"


def test_long_line_is_split_at_sentence_ends():
    text = "هذه جملة أولى طويلة قليلا. وهذه جملة ثانية أطول منها بقليل. وهذه الثالثة."
    parts = split_text(text, 40)
    assert len(parts) >= 2 and all(len(p) <= 40 for p in parts)
    assert " ".join(parts) == text


@needs_ffmpeg
def test_long_line_joined_with_silence():
    tts = FakeTTS()
    tts.max_chars = 30
    al, _, rep = produce(one_line("هذه جملة أولى قصيرة. وهذه جملة ثانية قصيرة."), tts=tts)
    assert len(tts.calls) == 2 and rep.passed
    assert al.duration_ms >= 3200 + 100  # 8 words of 0.4 s plus the 120 ms joint


@needs_ffmpeg
def test_silent_audio_is_a_blocker():
    _, _, rep = produce(one_line("في مساء يوم جمعة كانت نادية تجهز البيتزا"), tts=FakeTTS(silent=True))
    assert any(i.code == "AUDIO_SILENT" and i.severity == "blocker" for i in rep.issues)


@needs_ffmpeg
def test_round_trip_bad_resynthesizes_twice_then_major():
    tts = FakeTTS()
    _, _, rep = produce(
        one_line("في مساء يوم جمعة كانت نادية تجهز البيتزا"), tts=tts, stt=FakeSTT(text="كلام مختلف تماما لا علاقة له")
    )
    assert len(tts.calls) == 3
    assert any(i.code == "AUDIO_MISMATCH" and i.severity == "major" for i in rep.issues)


@needs_ffmpeg
def test_wer_around_0_3_is_a_minor_warning():
    spoken = "واحد اثنان ثلاثة أربعة خمسة ستة سبعة ثمانية تسعة عشرة"
    heard = "واحد اثنان ثلاثة أربعة خمسة ستة سبعة بيت قلم ورقة"  # 3 of 10 wrong
    _, _, rep = produce(one_line(spoken), stt=FakeSTT(text=heard))
    issue = next(i for i in rep.issues if i.code == "AUDIO_MISMATCH")
    assert issue.severity == "minor" and rep.passed


def test_digits_in_display_get_one_word_entry():
    display, spoken = "أكلنا 1/4 البيتزا", "أكلنا واحد من أربعة البيتزا"
    asr = [{"w": w, "s": i * 400, "e": (i + 1) * 400} for i, w in enumerate(spoken.split())]
    words, source = align_words(display, spoken, asr, 2000)
    assert source == "stt" and [w.w for w in words] == ["أكلنا", "1/4", "البيتزا"]
    frac = words[1]
    assert frac.s == 400 and frac.e == 1600  # spans "واحد من أربعة"


def test_no_word_times_gives_estimated():
    words, source = align_words("في مساء يوم جمعة", "في مساء يوم جمعة", [], 1600)
    assert source == "estimated" and len(words) == 4 and words[-1].e <= 1600


@needs_ffmpeg
def test_speech_rate_too_fast_is_major():
    line = one_line(" ".join(["كلمة"] * 30))
    _, _, rep = produce(line, tts=FakeTTS(seconds_per_word=4 / 30))
    assert any(i.code == "SPEECH_RATE" and i.severity == "major" for i in rep.issues)


def test_measured_wps_for_calibration():
    # 9.7: the golden audio was made at 0.4 s per spoken word, so the story-wide rate is close to 2.5
    wps = s6_audio.measured_wps(golden.script(), golden.audio())
    assert 2.0 <= wps <= 2.6
    assert s6_audio.measured_wps(golden.script(), golden.audio().model_copy(update={"lines": []})) == 0.0
