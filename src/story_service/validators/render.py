"""S11 render validator (Section 8.3): the bundle in headless Chromium, served with the production headers.

For each viewport the page is loaded with `?freeze=1`; the validator seeks to the sample times, waits two animation
frames, screenshots the stage and reads `__story.getFrameInfo()`. The checks below turn those frames into the
Section 8.1 report format, one report per scene plus one for the whole bundle. Every failed check carries the stage
that most likely caused it (`suspect_stage`), which the repair loop (`stages/s11_repair.py`) acts on.

Pixel work uses Pillow only. All times are story time in ms."""

from __future__ import annotations

import io
import json
import math
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from PIL import Image, ImageChops, ImageStat

from ..pipeline.models import ValidationReport

VIEWPORTS = {"V1": (1280, 720), "V2": (844, 390), "V3": (390, 844)}
TWEENS = {
    "show",
    "hide",
    "fade",
    "move",
    "scale",
    "rotate",
    "keyframes",
    "highlight",
    "shake",
    "camera",
    "enter",
    "exit",
}
STAGE = (1280, 720)
BOUNDS_MARGIN = 40
MOTION_MIN, STATIC_MAX, STATIC_WINDOW_MS = 0.01, 0.002, 6000
PIXEL_DELTA = 12
MAX_OVERLAP = 0.35
MIN_CAPTION_PX, MIN_CONTRAST, MIN_SVG_TEXT_PX, MIN_TOUCH_PX = 16, 7.0, 12, 44
MAX_DOM_NODES = 3500
AUDIO_SYNC_MS, AUDIO_POINTS = 150, 5
STATIC_GRID_MS = 2000  # extra samples so a 6 s static stretch is always seen (the spec's samples can be sparse)

# a hook kept by the page so the audio sync check can read each line's audio position
AUDIO_HOOK = """
window.__audios = {}; window.__audioErrors = [];
const __Audio = window.Audio;
window.Audio = function () {
  const a = new __Audio(...arguments);
  a.addEventListener('error', () => window.__audioErrors.push((a.src || '').split('/').pop()));
  return a;
};
window.Audio.prototype = __Audio.prototype;
const __play = HTMLMediaElement.prototype.play;
HTMLMediaElement.prototype.play = function () {
  window.__audios[this.src.split('/').pop()] = this;
  return __play.call(this);
};
"""


# ================================================================================================= pixels
def png_image(data: bytes) -> Image.Image:
    return Image.open(io.BytesIO(data)).convert("RGB")


def diff_ratio(a: Image.Image, b: Image.Image, delta: int = PIXEL_DELTA) -> float:
    """Share of pixels where some channel differs by more than `delta` (of 255)."""
    if a.size != b.size:
        b = b.resize(a.size)
    r, g, bl = ImageChops.difference(a, b).split()
    biggest = ImageChops.lighter(ImageChops.lighter(r, g), bl)
    hist = biggest.histogram()
    return sum(hist[delta + 1 :]) / (a.size[0] * a.size[1])


def gray_stddev(img: Image.Image) -> float:
    return ImageStat.Stat(img.convert("L")).stddev[0]


def contrast_ratio(fg: tuple[float, float, float], bg: tuple[float, float, float]) -> float:
    """WCAG 2 contrast ratio of two sRGB colors (0-255)."""

    def lum(c):
        ch = [v / 255 for v in c]
        ch = [v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4 for v in ch]
        return 0.2126 * ch[0] + 0.7152 * ch[1] + 0.0722 * ch[2]

    hi, lo = sorted((lum(fg), lum(bg)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def fps_verdict(fps: float) -> tuple[bool, str]:
    """(ok, severity) for the performance check: >= 24 passes, 20-24 minor, below 20 major."""
    if fps >= 24:
        return True, "minor"
    return False, "minor" if fps >= 20 else "major"


def overlap_ratio(a, b) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    small = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1]))
    return ix * iy / small if small > 0 else 0.0


# ================================================================================================= the story timeline
@dataclass
class Sample:
    t: int
    scene_id: str
    kind: str  # "start", "cue", "interaction", "end", "grid", "story"
    image: Image.Image | None = None
    frame: dict = field(default_factory=dict)


def sample_times(data: dict, grid_ms: int | None = STATIC_GRID_MS) -> list[tuple[int, str, str]]:
    """(t, scene_id, kind) per Section 8.3: scene start + 500, every tween cue at start + min(dur/2, 600), every
    interaction time, scene end - 300, plus T = 0 and the story end; optionally a grid every `grid_ms`."""
    out = [(0, data["scenes"][0]["id"], "story")]
    for s in data["scenes"]:
        start, end = s["start_ms"], s["end_ms"]
        out.append((start + 500, s["id"], "start"))
        for c in s["cues"]:
            if c["action"] in TWEENS:
                out.append((c["t"] + min(c["dur"] // 2, 600), s["id"], "cue"))
        if s.get("interaction"):
            out.append((s["interaction"]["at_ms"], s["id"], "interaction"))
        out.append((end - 300, s["id"], "end"))
        if grid_ms:
            out += [(t, s["id"], "grid") for t in range(start + grid_ms, end - 300, grid_ms)]
    last = data["scenes"][-1]
    out.append((data["meta"]["duration_ms"] - 1, last["id"], "story"))
    seen, uniq = set(), []
    for t, sid, kind in sorted(out):
        if (t, sid) not in seen:
            seen.add((t, sid))
            uniq.append((t, sid, kind))
    return uniq


def walk_windows(data: dict) -> dict[str, list[tuple[int, int]]]:
    """element id -> [(start, end)] when it is offstage or walking: from the scene start until its `enter` ends, and
    from its `exit` until the scene ends. Bounds and overlap do not apply then."""
    out: dict[str, list[tuple[int, int]]] = {}
    for s in data["scenes"]:
        for c in s["cues"]:
            if c["action"] == "enter":
                out.setdefault(c["target"], []).append((s["start_ms"], c["t"] + max(c["dur"], 1)))
            elif c["action"] == "exit":
                out.setdefault(c["target"], []).append((c["t"], s["end_ms"]))
    return out


def scene_at(data: dict, t: int) -> str:
    for s in data["scenes"]:
        if t < s["end_ms"]:
            return s["id"]
    return data["scenes"][-1]["id"]


# ================================================================================================= the page
class StoryPage:
    """One viewport of the bundle: errors, network, seeking, frames."""

    def __init__(
        self,
        browser,
        url: str,
        viewport: tuple[int, int],
        *,
        reduced_motion: bool = False,
        init_scripts: tuple[str, ...] = (AUDIO_HOOK,),
        mobile: bool | None = None,
    ):
        w, h = viewport
        mobile = w < 900 if mobile is None else mobile
        self.ctx = browser.new_context(
            viewport={"width": w, "height": h},
            is_mobile=mobile,
            has_touch=mobile,
            reduced_motion="reduce" if reduced_motion else "no-preference",
        )
        for script in init_scripts:
            self.ctx.add_init_script(script)
        origin = "{0.scheme}://{0.netloc}".format(urlsplit(url))
        self.errors: list[str] = []
        self.outside: list[str] = []
        self.failed: list[str] = []

        def route(r):
            if r.request.url.startswith(origin) or r.request.url.startswith("data:"):
                r.continue_()
            else:
                self.outside.append(r.request.url)
                r.abort()

        self.ctx.route("**/*", route)
        self.page = self.ctx.new_page()
        self.page.on("console", lambda m: self.errors.append(f"console: {m.text}") if m.type == "error" else None)
        self.page.on("pageerror", lambda e: self.errors.append(f"page error: {e}"))
        self.page.on(
            "requestfailed",
            lambda r: (
                self.failed.append(f"{r.url} ({r.failure})")
                if r.url.startswith(origin) and "abort" not in str(r.failure).lower()
                else None
            ),
        )
        self.page.goto(url + ("&" if "?" in url else "?") + "freeze=1")
        self.wait_until("window.__story !== undefined", 10000)
        self.data = json.loads(self.page.evaluate("document.getElementById('story-data').textContent"))

    def wait_until(self, expr: str, timeout_ms: int = 3000, step_ms: int = 50) -> bool:
        """Poll `expr` from Python. (Playwright's wait_for_function compiles the expression inside the page, which
        the production CSP forbids: it would show up as a page error of our own making.)"""
        waited = 0
        while waited <= timeout_ms:
            if self.page.evaluate(expr):
                return True
            self.page.wait_for_timeout(step_ms)
            waited += step_ms
        return False

    def close(self):
        self.ctx.close()

    def js(self, expr: str):
        return self.page.evaluate(expr)

    def seek(self, t: int) -> None:
        self.page.evaluate(
            "t => new Promise(r => { window.__story.seek(t); requestAnimationFrame(() => requestAnimationFrame(r)); })",
            t,
        )

    def stage_rect(self) -> dict:
        return self.page.evaluate(
            "() => { const r = document.getElementById('stage').getBoundingClientRect();"
            " return {x: r.left, y: r.top, width: r.width, height: r.height}; }"
        )

    def stage_image(self) -> Image.Image:
        r = self.stage_rect()
        return png_image(self.page.screenshot(clip=r))

    def frame_info(self) -> dict:
        return self.page.evaluate("window.__story.getFrameInfo()")

    def state(self) -> dict:
        return self.page.evaluate("window.__story.getState()")


# ================================================================================================= the checks
class Reports:
    """One ValidationReport per scene plus one for the bundle, with helpers that keep check names unique."""

    def __init__(self, scene_ids: list[str]):
        self.scenes = {sid: ValidationReport(stage="S11", scene_id=sid) for sid in scene_ids}
        self.bundle = ValidationReport(stage="S11")

    def of(self, scene_id: str | None) -> ValidationReport:
        return self.scenes.get(scene_id, self.bundle) if scene_id else self.bundle

    def covers(self, scene_id: str | None) -> bool:
        """False for a scene outside a partial re-validation: its checks are skipped, not filed under the bundle."""
        return scene_id is None or scene_id in self.scenes

    def all(self) -> list[ValidationReport]:
        return [*self.scenes.values(), self.bundle]


def collect_samples(sp: StoryPage, times: list[tuple[int, str, str]], with_images: bool = True) -> list[Sample]:
    out = []
    for t, sid, kind in times:
        sp.seek(t)
        out.append(
            Sample(t=t, scene_id=sid, kind=kind, image=sp.stage_image() if with_images else None, frame=sp.frame_info())
        )
    return out


def check_errors_network(rep: Reports, sp: StoryPage, vp: str) -> None:
    errors = sp.errors + [f"player: {e}" for e in sp.state().get("errors", [])]
    broken = sp.js("window.__audioErrors || []")
    rep.bundle.add(
        f"audio_loads_{vp}",
        not broken,
        "blocker",
        f"audio files that do not load or decode: {broken[:4]}",
        code="AUDIO_BROKEN",
        suspect_stage="S6_audio",
    )
    rep.bundle.add(
        f"errors_{vp}", not errors, "blocker", "; ".join(errors[:4]), code="PAGE_ERRORS", suspect_stage="S10_assembler"
    )
    bad = sp.outside + sp.failed
    rep.bundle.add(
        f"network_{vp}",
        not bad,
        "blocker",
        f"requests outside the page or failed: {bad[:4]}",
        code="NETWORK",
        suspect_stage="S10_assembler",
    )


def check_blank(rep: Reports, samples: list[Sample], vp: str) -> None:
    for s in samples:
        if s.image is None or s.kind == "story":
            continue
        sd = gray_stddev(s.image)
        rep.of(s.scene_id).add(
            f"blank_{vp}_{s.t}",
            sd >= 3,
            "blocker",
            f"blank stage at t={s.t} (gray std {sd:.1f})",
            code="BLANK_FRAME",
            suspect_stage="S7_director",
            fix_hint="make sure the scene shows its background",
        )


def check_motion(rep: Reports, data: dict, samples: list[Sample], vp: str) -> None:
    by_t = {(s.scene_id, s.t): s for s in samples}
    for sc in data["scenes"]:
        if not any(c["action"] in TWEENS for c in sc["cues"]):
            continue
        a, b = by_t.get((sc["id"], sc["start_ms"] + 500)), by_t.get((sc["id"], sc["end_ms"] - 300))
        if not a or not b or a.image is None or b.image is None:
            continue
        r = diff_ratio(a.image, b.image)
        rep.of(sc["id"]).add(
            f"motion_{vp}",
            r >= MOTION_MIN,
            "major",
            f"start and end frames differ in only {r:.2%} of pixels",
            code="NO_MOTION",
            suspect_stage="S7_director",
            fix_hint="make something visibly change in the scene",
        )


def check_static_runs(rep: Reports, data: dict, samples: list[Sample], vp: str) -> None:
    for sc in data["scenes"]:
        seq = sorted((s for s in samples if s.scene_id == sc["id"] and s.image is not None), key=lambda s: s.t)
        run_start, worst = None, (0, 0, 0)
        for prev, cur in zip(seq, seq[1:]):
            if diff_ratio(prev.image, cur.image) < STATIC_MAX:
                run_start = prev.t if run_start is None else run_start
                if cur.t - run_start > worst[0]:
                    worst = (cur.t - run_start, run_start, cur.t)
            else:
                run_start = None
        rep.of(sc["id"]).add(
            f"static_run_{vp}",
            worst[0] < STATIC_WINDOW_MS,
            "major",
            f"nothing visibly changes from t={worst[1]} to t={worst[2]} ({worst[0] / 1000:.1f} s)",
            code="STATIC_RUN",
            suspect_stage="S7_director",
            fix_hint=f"add a pose, expression, highlight or move between t={worst[1]} and t={worst[2]}",
        )


def _in_window(windows, el_id, t) -> bool:
    return any(a <= t <= b for a, b in windows.get(el_id, []))


def check_bounds_overlap(rep: Reports, data: dict, samples: list[Sample], vp: str) -> None:
    windows = walk_windows(data)
    lo_x, lo_y = -BOUNDS_MARGIN, -BOUNDS_MARGIN
    hi_x, hi_y = STAGE[0] + BOUNDS_MARGIN, STAGE[1] + BOUNDS_MARGIN
    out: dict[tuple[str, str], tuple[float, int]] = {}
    over: dict[tuple[str, str, str], tuple[float, int]] = {}
    for s in samples:
        items = [x for x in s.frame.get("actors", []) + s.frame.get("props", []) if x.get("opacity", 1) > 0.05]
        for it in items:
            if _in_window(windows, it["id"], s.t):
                continue
            x0, y0, x1, y1 = it["bbox"]
            excess = max(lo_x - x0, lo_y - y0, x1 - hi_x, y1 - hi_y, 0)
            if excess > out.get((s.scene_id, it["key"]), (0, 0))[0]:
                out[(s.scene_id, it["key"])] = (excess, s.t)
        actors = [a for a in s.frame.get("actors", []) if not _in_window(windows, a["id"], s.t)]
        for i, a in enumerate(actors):
            for b in actors[i + 1 :]:
                r = overlap_ratio(a["bbox"], b["bbox"])
                key = (s.scene_id, a["key"], b["key"])
                if r > over.get(key, (0, 0))[0]:
                    over[key] = (r, s.t)
    for sid in rep.scenes:
        bad = [(k, v) for k, v in out.items() if k[0] == sid]
        for (_, key), (excess, t) in bad:
            rep.of(sid).add(
                f"bounds_{vp}_{key}",
                False,
                "major",
                f"{key} exceeds the stage by {excess:.0f} px at t={t}",
                code="OUT_OF_BOUNDS",
                suspect_stage="S7_director",
                fix_hint=f"keep {key} (and its moves) inside x 40-1240 and y 40-700",
            )
        if not bad:
            rep.of(sid).add(f"bounds_{vp}", True, "major")
        worst = [(k, v) for k, v in over.items() if k[0] == sid and v[0] > MAX_OVERLAP]
        for (_, ka, kb), (r, t) in worst:
            rep.of(sid).add(
                f"overlap_{vp}_{ka}_{kb}",
                False,
                "major",
                f"{ka} and {kb} overlap {r:.0%} at t={t}",
                code="OVERLAP",
                suspect_stage="S7_director",
                fix_hint=f"move {ka} and {kb} at least 220 px apart",
            )
        if not worst:
            rep.of(sid).add(f"overlap_{vp}", True, "major")


def check_captions(rep: Reports, sp: StoryPage, vp: str) -> None:
    """Size and contrast of the captions on the longest line of each scene (the one most likely to shrink)."""
    for sc in sp.data["scenes"]:
        if not sc["lines"] or not rep.covers(sc["id"]):
            continue
        line = max(sc["lines"], key=lambda ln: len(ln["text"]))
        sp.seek(line["start_ms"] + 50)
        info = sp.js(
            """() => { const c = document.getElementById('captions'), w = c.querySelector('.w');
            const cs = getComputedStyle(c), fg = getComputedStyle(w || c).color;
            let bg = cs.backgroundColor, el = c;
            while (el && /rgba\\(0, 0, 0, 0\\)|transparent/.test(bg)) { el = el.parentElement; bg = el ? getComputedStyle(el).backgroundColor : 'rgb(255,255,255)'; }
            const b = c.firstChild; let lines = 0;
            if (b) { b.style.display = 'block'; b.style.webkitLineClamp = 'unset';
                     lines = b.scrollHeight / (parseFloat(cs.fontSize) * 1.6);
                     b.style.display = ''; b.style.webkitLineClamp = ''; }
            return {size: parseFloat(cs.fontSize), fg, bg, lines, hidden: c.classList.contains('off')}; }"""
        )
        fg, bg = _rgb(info["fg"]), _rgb(info["bg"])
        ratio = contrast_ratio(fg, bg)
        r = rep.of(sc["id"])
        r.add(
            f"caption_size_{vp}",
            info["size"] >= MIN_CAPTION_PX,
            "blocker",
            f"captions are {info['size']:.0f} px (at least {MIN_CAPTION_PX})",
            code="CAPTION_SIZE",
            suspect_stage="S10_assembler",
        )
        r.add(
            f"caption_contrast_{vp}",
            ratio >= MIN_CONTRAST,
            "blocker",
            f"caption contrast {ratio:.1f}:1 (at least {MIN_CONTRAST:.0f}:1)",
            code="CAPTION_CONTRAST",
            suspect_stage="S10_assembler",
        )
        r.add(
            f"caption_fits_{vp}",
            info["lines"] <= 2.1,
            "minor",
            f"line {line['id']} needs {info['lines']:.1f} lines at the smallest caption size (2 allowed)",
            code="CAPTION_TOO_LONG",
            suspect_stage="S4_writer",
            line_id=line["id"],
            fix_hint="shorten the line or split it in two",
        )


def _rgb(css: str) -> tuple[float, float, float]:
    nums = [float(x) for x in css[css.index("(") + 1 : css.index(")")].replace("/", ",").split(",")[:3]]
    return nums[0], nums[1], nums[2]


def check_svg_text(rep: Reports, sp: StoryPage, samples: list[Sample], vp: str) -> None:
    """Effective size of every visible SVG text (widget labels) on this viewport."""
    worst: dict[str, tuple[float, str, int]] = {}
    for s in samples:
        if s.kind not in ("start", "cue", "end"):
            continue
        sp.seek(s.t)
        texts = sp.js(
            """() => { const out = [], sc = document.querySelector('svg.scene.active'); if (!sc) return out;
            sc.querySelectorAll('text').forEach(t => {
              const r = t.getBoundingClientRect(), b = t.getBBox(); if (!r.width || !b.width) return;
              let o = 1; for (let e = t; e && e !== sc; e = e.parentElement) o *= parseFloat(getComputedStyle(e).opacity);
              if (o < 0.05) return;
              out.push({text: t.textContent, px: parseFloat(t.getAttribute('font-size') || 16) * r.width / b.width}); });
            return out; }"""
        )
        for x in texts:
            if x["px"] < worst.get(s.scene_id, (1e9, "", 0))[0]:
                worst[s.scene_id] = (x["px"], x["text"], s.t)
    for sid, (px, text, t) in worst.items():
        rep.of(sid).add(
            f"svg_text_{vp}",
            px >= MIN_SVG_TEXT_PX - 0.25,
            "major",
            f"label {text!r} is {px:.1f} px at t={t} (at least {MIN_SVG_TEXT_PX})",
            code="SVG_TEXT_SMALL",
            suspect_stage="S7_director",
            fix_hint="raise the widget's scale",
        )


def check_touch_controls(rep: Reports, sp: StoryPage, vp: str) -> None:
    small = sp.js(
        """() => [...document.querySelectorAll('#controls button, #controls [role=slider], #start button')]
           .filter(b => b.offsetParent).map(b => [b.id || b.className, b.getBoundingClientRect()])
           .filter(([, r]) => r.width < 43.5 || r.height < 43.5)
           .map(([n, r]) => `${n} ${Math.round(r.width)}x${Math.round(r.height)}`)"""
    )
    rep.bundle.add(
        f"touch_targets_{vp}",
        not small,
        "major",
        f"controls under 44x44 px: {small[:4]}",
        code="TOUCH_TARGET",
        suspect_stage="S10_assembler",
    )


def check_portrait(rep: Reports, sp: StoryPage, vp: str) -> None:
    info = sp.js(
        """() => { const ids = ['stage', 'captions', 'controls'], H = innerHeight, W = innerWidth;
        const out = ids.filter(id => { const r = document.getElementById(id).getBoundingClientRect();
          return r.bottom > H + 1 || r.top < -1 || r.right > W + 1 || r.left < -1; });
        return {out, scroll: document.scrollingElement.scrollHeight > H + 1 || document.scrollingElement.scrollWidth > W + 1}; }"""
    )
    ok = not info["out"] and not info["scroll"]
    rep.bundle.add(
        f"portrait_{vp}",
        ok,
        "major",
        f"not visible without scrolling: {info}",
        code="PORTRAIT",
        suspect_stage="S10_assembler",
    )


def check_dom(rep: Reports, sp: StoryPage) -> None:
    n = sp.js("document.getElementsByTagName('*').length")
    rep.bundle.add(
        "dom_size",
        n <= MAX_DOM_NODES,
        "major",
        f"{n} DOM nodes (max {MAX_DOM_NODES})",
        code="DOM_SIZE",
        suspect_stage="S10_assembler",
    )


def check_audio_sync(rep: Reports, sp: StoryPage, max_lines: int | None = None) -> None:
    """At 5 points per line, the audio position differs from T - line.start by at most 150 ms (real playback)."""
    lines = [(sc["id"], ln) for sc in sp.data["scenes"] for ln in sc["lines"] if rep.covers(sc["id"])]
    if max_lines is not None:
        step = max(1, len(lines) // max_lines)
        lines = lines[::step][:max_lines]
    for sid, ln in lines:
        sp.js("window.__story.pause()")
        sp.seek(ln["start_ms"])
        sp.js("window.__story.play()")
        dur, worst, measured = ln["end_ms"] - ln["start_ms"], 0.0, 0
        name = ln["audio"].split("/")[-1]
        for _ in range(AUDIO_POINTS):
            sp.page.wait_for_timeout(max(50, dur / (AUDIO_POINTS + 1)))
            m = sp.js(
                f"""() => {{ const a = window.__audios[{json.dumps(name)}], t = window.__story.getTime();
                if (!a || a.paused || a.readyState < 3) return null;
                return {{off: Math.abs(a.currentTime * 1000 - (t - {ln["start_ms"]})), t}}; }}"""
            )
            if m is not None and m["t"] < ln["end_ms"] - 50:
                worst, measured = max(worst, m["off"]), measured + 1
        sp.js("window.__story.pause()")
        if measured == 0:  # the line's audio never played: nothing to compare, and nothing a student hears
            rep.of(sid).add(
                f"audio_sync_{ln['id']}",
                False,
                "major",
                f"{ln['id']}: the audio did not play",
                code="AUDIO_NOT_PLAYING",
                suspect_stage="S6_audio",
                line_id=ln["id"],
            )
            continue
        rep.of(sid).add(
            f"audio_sync_{ln['id']}",
            worst <= AUDIO_SYNC_MS,
            "major",
            f"{ln['id']}: audio off by {worst:.0f} ms",
            code="AUDIO_SYNC",
            suspect_stage="S10_assembler",
            line_id=ln["id"],
        )


def check_performance(rep: Reports, sp: StoryPage, cpu_throttle: float = 4.0, seconds: float = 5.0) -> float:
    counts = sp.js(
        """() => [...document.querySelectorAll('svg.scene')].map(s => [s.id, s.getElementsByTagName('*').length])"""
    )
    busiest = max(counts, key=lambda x: x[1])[0]
    scene = next(s for s in sp.data["scenes"] if s["svg_id"] == busiest)
    cdp = sp.ctx.new_cdp_session(sp.page)
    cdp.send("Emulation.setCPUThrottlingRate", {"rate": cpu_throttle})
    sp.js("document.documentElement.classList.remove('freeze')")  # measure real playback: idle and ambient loops on
    try:
        sp.seek(scene["start_ms"])
        sp.js("window.__story.play()")
        intervals = sp.page.evaluate(
            """ms => new Promise(done => { const out = []; let last = performance.now(); const end = last + ms;
            function f(now) { out.push(now - last); last = now; if (now < end) requestAnimationFrame(f); else done(out); }
            requestAnimationFrame(f); })""",
            int(seconds * 1000),
        )
        sp.js("window.__story.pause()")
    finally:
        cdp.send("Emulation.setCPUThrottlingRate", {"rate": 1})
        sp.js("document.documentElement.classList.add('freeze')")
    mean = sum(intervals[1:]) / max(1, len(intervals) - 1)
    fps = 1000 / mean if mean > 0 else 0.0
    ok, severity = fps_verdict(fps)
    rep.bundle.add(
        "performance",
        ok,
        severity,
        f"{fps:.1f} fps on {scene['id']} under {cpu_throttle:g}x CPU throttle",
        code="SLOW",
        suspect_stage="S10_assembler",
    )
    rep.bundle.scores["fps"] = round(fps, 1)
    return fps


def check_reduced_motion(rep: Reports, browser, url: str) -> None:
    sp = StoryPage(browser, url, VIEWPORTS["V1"], reduced_motion=True)
    try:
        for sc in sp.data["scenes"]:  # every scene builds its tweens when first shown
            sp.seek(sc["start_ms"] + 100)
        long_tweens = sp.js(
            """() => document.getAnimations().filter(a => !(window.CSSAnimation && a instanceof CSSAnimation)
               && !(window.CSSTransition && a instanceof CSSTransition))
               .map(a => a.effect.getTiming().duration).filter(d => d > 50).length"""
        )
        sp.js("window.__story.play()")
        sp.page.wait_for_timeout(800)
        errors = sp.errors + sp.state().get("errors", [])
        rep.bundle.add(
            "reduced_motion",
            not errors and long_tweens == 0,
            "minor",
            f"{long_tweens} tweens longer than 50 ms; errors: {errors[:3]}",
            code="REDUCED_MOTION",
            suspect_stage="S10_assembler",
        )
    finally:
        sp.close()


def frames_for_critic(samples: list[Sample], scene_id: str, n: int = 6) -> list[Image.Image]:
    """Up to `n` frames of a scene spread over its time, preferring the spec's own sample points."""
    pool = [s for s in samples if s.scene_id == scene_id and s.image is not None and s.kind != "grid"]
    pool = pool or [s for s in samples if s.scene_id == scene_id and s.image is not None]
    if len(pool) <= n:
        return [s.image for s in pool]
    if n == 1:
        return [pool[len(pool) // 2].image]
    step = (len(pool) - 1) / (n - 1)
    return [pool[round(i * step)].image for i in range(n)]


def to_png(img: Image.Image, width: int = 768) -> bytes:
    h = math.ceil(img.size[1] * width / img.size[0])
    buf = io.BytesIO()
    img.resize((width, h)).save(buf, format="PNG")
    return buf.getvalue()
