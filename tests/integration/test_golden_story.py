"""The golden story end to end (S6 -> S10 with fakes), then played in a real Chromium under the production headers.

Needs ffmpeg; the browser part needs Playwright with Chromium and is skipped without them."""

import json
import shutil
from functools import lru_cache
from pathlib import Path

import pytest

import golden
from story_service.devtools.bundle_server import serving
from story_service.pipeline import module6
from story_service.providers.llm import FakeLLM
from story_service.providers.stt import FakeSTT
from story_service.providers.tts import FakeTTS
from story_service.providers.vlm import FakeVLM

pytestmark = pytest.mark.integration
if shutil.which("ffmpeg") is None:
    pytest.skip("ffmpeg is not installed", allow_module_level=True)


@lru_cache(maxsize=1)
def built() -> tuple[module6.Module6Result, str]:
    res = module6.run(
        golden.script(),
        golden.cast(),
        golden.intake(),
        tts=FakeTTS(),
        stt=FakeSTT(),
        llm=FakeLLM("s8_custom_pizza_cutter.json"),
        vlm=FakeVLM(score=5),
        plans=golden.plans(),
        generated_at="2026-10-21T08:00:00Z",
    )
    import tempfile

    folder = tempfile.mkdtemp(prefix="golden_bundle_")
    for rel, data in res.bundle.files().items():
        p = Path(folder) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return res, folder


def test_module6_end_to_end():
    res, folder = built()
    assert all(r.passed for r in res.reports.values()), {k: r.issues for k, r in res.reports.items() if not r.passed}
    m = res.bundle.meta
    assert m.scene_count == 5 and m.interaction_count == 3 and m.sizes.audio_files == 32
    assert 127_500 <= m.duration_ms <= 172_500
    assert (Path(folder) / "index.html").exists() and (Path(folder) / "audio" / "s01_l01.mp3").exists()
    assert json.loads((Path(folder) / "meta.json").read_text())["title"] == "بيتزا نادية العادلة"


def _browser():
    sync_api = pytest.importorskip("playwright.sync_api")
    return sync_api.sync_playwright()


def test_player_in_chromium():
    _, folder = built()
    with _browser() as p, serving(folder) as url:
        browser = p.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        errors, outside = [], []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("request", lambda r: outside.append(r.url) if not r.url.startswith(url) else None)
        page.goto(url + "/index.html?freeze=1")
        s = page.evaluate("window.__story")
        assert s is not None
        dur = page.evaluate("window.__story.getDuration()")
        assert dur == built()[0].timeline.meta.duration_ms

        # seeking is exact and shows the right scene
        for t, scene in ((1000, "s01"), (20000, "s02"), (130000, "s05")):
            page.evaluate(f"window.__story.seek({t})")
            st = page.evaluate("window.__story.getState()")
            assert st["t"] == t and st["activeScene"] == scene

        # actors stand where the plan put them (feet near y 640), inside the stage
        page.evaluate("window.__story.seek(1500)")
        info = page.evaluate("window.__story.getFrameInfo()")
        for a in info["actors"]:
            assert 600 <= a["bbox"][3] <= 660 and a["bbox"][0] >= 0 and a["bbox"][2] <= 1280

        # the three interactions open and can be completed through the hooks
        assert [i["id"] for i in page.evaluate("window.__story.listInteractions()")] == ["s02_i1", "s03_i1", "s04_i1"]
        assert page.evaluate("window.__story.openInteraction('s02_i1')")
        for el in ("s02-pizza_small", "s02-pizza_big"):
            assert page.evaluate(f"window.__story.answerInteraction('s02_i1', {{reveal: '{el}'}})")
        assert page.locator("#overlay .go").is_visible()
        assert page.evaluate("window.__story.openInteraction('s03_i1')")
        page.evaluate("window.__story.answerInteraction('s03_i1', {option_id: 'b'})")
        assert page.locator("#overlay .opt.ok").count() == 1
        assert page.evaluate("window.__story.openInteraction('s04_i1')")
        for pair in ("p1", "p2"):
            page.evaluate(
                f"window.__story.answerInteraction('s04_i1', {{drop: {{item_id: '{pair}', target_id: '{pair}'}}}})"
            )
        assert page.locator("#overlay .go").is_visible()

        # real playback: the clock runs, the audio plays under the production CSP, nothing leaves the origin
        page.evaluate("window.__story.seek(0)")
        page.evaluate("window.__story.play()")
        page.wait_for_timeout(1500)
        st = page.evaluate("window.__story.getState()")
        assert st["playing"] and 900 <= st["t"] <= 2500
        assert not outside, outside
        assert not errors and not st["errors"], (errors, st["errors"])
        browser.close()


def test_player_on_a_phone():
    _, folder = built()
    with _browser() as p, serving(folder) as url:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
        page.goto(url + "/index.html?freeze=1")
        page.evaluate("window.__story.openInteraction('s03_i1')")
        sizes = page.evaluate("""() => [...document.querySelectorAll('#controls .ctl, #overlay button')]
            .filter(b => !b.hidden && b.offsetParent).map(b => [b.offsetWidth, b.offsetHeight])""")
        assert sizes and min(min(s) for s in sizes) >= 48  # touch targets at least 48 px
        assert page.evaluate("document.documentElement.scrollWidth") <= 390  # no sideways scroll
        browser.close()
