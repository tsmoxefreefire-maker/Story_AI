"""S11 render validation (Sections 8.3 to 8.5): the bundle in headless Chromium on three viewports.

    rep = s11_validate.run(url, vlm=vlm, script=script, cast=cast)          # all viewports, all checks
    rep = s11_validate.run(url, viewport=(390, 844))                         # one viewport
    rep.blockers, rep.majors, rep.passed, rep.to_json()                      # validation.json (8.1 format)

V1 1280x720 and V2 844x390 get every check; V3 390x844 gets the legibility, touch and portrait checks (8.3). The
VLM critic looks at the V1 frames. `scenes=` limits the scene checks to the scenes a repair touched (the bundle
checks always run), `checks=` limits the work in tests."""

from __future__ import annotations

from dataclasses import dataclass, field

from ...validators import interaction as it_tests
from ...validators import render as rv
from ...validators import vlm_critic
from ..models import Cast, Issue, Script, ValidationReport

ALL_CHECKS = frozenset({
    "errors", "blank", "motion", "static", "bounds", "captions", "svg_text", "touch", "portrait", "dom", "audio",
    "performance", "reduced_motion", "interactions", "playthrough", "vlm",
})  # fmt: skip
V3_CHECKS = {"errors", "captions", "svg_text", "touch", "portrait", "interactions"}


@dataclass
class S11Result:
    reports: list[ValidationReport] = field(default_factory=list)

    @property
    def issues(self) -> list[Issue]:
        return [i for r in self.reports for i in r.issues]

    @property
    def blockers(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "blocker"]

    @property
    def majors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "major"]

    @property
    def minors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "minor"]

    @property
    def passed(self) -> bool:
        return not self.blockers and not self.majors

    def report(self, scene_id: str | None) -> ValidationReport | None:
        return next((r for r in self.reports if r.scene_id == scene_id), None)

    def to_json(self) -> list[dict]:
        return [r.model_dump() for r in self.reports]


def _viewports(viewport) -> list[tuple[str, tuple[int, int]]]:
    if viewport is None:
        return list(rv.VIEWPORTS.items())
    for name, size in rv.VIEWPORTS.items():
        if tuple(viewport) == size:
            return [(name, size)]
    return [("custom", tuple(viewport))]


def run(
    url: str,
    viewport: tuple[int, int] | None = None,
    *,
    vlm=None,
    script: Script | None = None,
    cast: Cast | None = None,
    age_group: str = "9-11",
    scenes: set[str] | None = None,
    checks: set[str] | frozenset[str] = ALL_CHECKS,
    audio_lines: int | None = None,
    browser=None,
) -> S11Result:
    """Validate the bundle served at `url` (it must be served with the production headers and HTTP Range, e.g. by
    `devtools.bundle_server.serving`). `audio_lines` limits the real-time audio check to that many lines."""
    from playwright.sync_api import sync_playwright

    own = browser is None
    pw = sync_playwright().start() if own else None
    browser = browser or pw.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
    try:
        return _run(browser, url, viewport, vlm, script, cast, age_group, scenes, set(checks), audio_lines)
    finally:
        if own:
            browser.close()
            pw.stop()


def _story_frames(browser, url, have: list[rv.Sample]) -> list[rv.Sample]:
    """One V1 frame from the middle of every scene that a partial run did not sample."""
    sampled = {s.scene_id for s in have}
    sp = rv.StoryPage(browser, url, rv.VIEWPORTS["V1"])
    try:
        missing = [
            (s["start_ms"] + (s["end_ms"] - s["start_ms"]) // 2, s["id"], "start")
            for s in sp.data["scenes"]
            if s["id"] not in sampled
        ]
        return rv.collect_samples(sp, missing) if missing else []
    finally:
        sp.close()


def _run(browser, url, viewport, vlm, script, cast, age_group, scenes, checks, audio_lines) -> S11Result:
    rep: rv.Reports | None = None
    v1_samples: list[rv.Sample] = []
    for vp, size in _viewports(viewport):
        legibility_only = vp == "V3"
        sp = rv.StoryPage(browser, url, size)
        try:
            if rep is None:
                ids = [s["id"] for s in sp.data["scenes"] if scenes is None or s["id"] in scenes]
                rep = rv.Reports(ids)
            data = sp.data
            times = [
                x
                for x in rv.sample_times(data, rv.STATIC_GRID_MS if vp == "V1" else None)
                if scenes is None or x[1] in scenes
            ]
            want_images = not legibility_only and checks & {"blank", "motion", "static", "vlm"}
            samples = rv.collect_samples(sp, times, with_images=bool(want_images))
            if vp == "V1":
                v1_samples = samples
            if not legibility_only:
                if "blank" in checks:
                    rv.check_blank(rep, samples, vp)
                if "motion" in checks:
                    rv.check_motion(rep, data, samples, vp)
                if "static" in checks and vp == "V1":
                    rv.check_static_runs(rep, data, samples, vp)
                if "bounds" in checks and vp == "V1":
                    rv.check_bounds_overlap(rep, data, samples, vp)
            if "captions" in checks:
                rv.check_captions(rep, sp, vp)
            if "svg_text" in checks and vp in ("V3", "custom"):
                rv.check_svg_text(rep, sp, samples, vp)
            if "touch" in checks and vp in ("V2", "V3", "custom"):
                rv.check_touch_controls(rep, sp, vp)
            if "portrait" in checks and vp == "V3":
                rv.check_portrait(rep, sp, vp)
            if "dom" in checks and vp in ("V1", "custom"):
                rv.check_dom(rep, sp)
            if "audio" in checks and vp in ("V1", "custom"):
                rv.check_audio_sync(rep, sp, audio_lines)
            if "performance" in checks and vp in ("V1", "custom"):
                rv.check_performance(rep, sp)
            if "errors" in checks:
                rv.check_errors_network(rep, sp, vp)
        finally:
            sp.close()
        if "interactions" in checks:
            it_tests.test_interactions(rep, browser, url, size, vp, full=vp in ("V1", "custom"))
    if "playthrough" in checks:
        it_tests.test_skip_and_playthrough(rep, browser, url, rv.VIEWPORTS["V1"])
    if "reduced_motion" in checks:
        rv.check_reduced_motion(rep, browser, url)
    if "vlm" in checks and vlm is not None and v1_samples:
        vlm_critic.critique_scenes(rep, v1_samples, vlm, script=script, cast=cast, age_group=age_group)
        scene_failed = any(
            i.code == "VLM_CRITIC" and i.severity in ("blocker", "major") for r in rep.scenes.values() for i in r.issues
        )
        if not scene_failed:  # 8.4: "after all scenes pass", one frame of EVERY scene, also on a partial run
            vlm_critic.critique_story(rep, v1_samples + _story_frames(browser, url, v1_samples), vlm)
    return S11Result(reports=rep.all() if rep else [])
