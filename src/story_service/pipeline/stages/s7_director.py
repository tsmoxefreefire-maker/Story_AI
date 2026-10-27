"""S7 Visual Director (large LLM, one call per scene) — Section 5, S7 and the director rules.

The LLM only fills a JSON plan (where everything stands, what moves when). This module:
  * builds the director's input (the scene's lines with their REAL durations and word timings, the cast present, the
    setting, the catalog) and calls the LLM;
  * checks the plan with code (`validate_s7`). Every rule the spec lists as checkable is a blocker here, so a bad plan
    goes back to the LLM with the failed checks instead of reaching the composer.

Times in this file are SCENE time: 0 is the start of the scene, the first line starts after the 600 ms lead-in
(the same clock S9 uses, see `s9_timeline.scene_clock`)."""

from __future__ import annotations

import json
from contextlib import suppress
from pathlib import Path

from pydantic import ValidationError

from ...assets.catalog import actor_stage_bbox, custom_ref, index, prop_stage_bbox, widget_stage_bbox
from ...assets.characters.build_character import EXPRESSIONS, POSES
from ...assets.widgets import MIN_TEXT, WIDGETS, render_widget, widget_texts
from ...validators.text import tokens
from ..models import STATE_ACTIONS, AudioOut, Cast, Scene, ScenePlan, Setting, ValidationReport
from .s8_composer import CUSTOM_BBOX
from .s9_timeline import UnresolvedCue, resolve_cue, scene_clock

# ------------------------------------------------------------------------------------------------- limits (spec)
MAX_ACTORS, MAX_ELEMENTS, MAX_CUES, MAX_CAMERA_MOVES = 4, 12, 40, 2
STAGE_W, STAGE_H = 1280, 720
SAFE_X, SAFE_Y = (40, 1240), (40, 700)
CHANGE_EVERY_MS = 6000  # rule 3: a visible change at least every 6 s of scene time
MAX_OVERLAP = 0.35  # rule 6: share of the smaller actor box
ZOOM = (1.0, 1.4)  # rule 8
MAX_LABEL_WORDS = 4  # rule 7
MIN_TAP_PX = 64  # tap_reveal targets
MAX_DUR_MS = 20000
EASINGS = {"linear", "ease-in", "ease-out", "ease-in-out"}
SIDES = {"left", "right"}
ACTOR_ONLY = {"pose", "expression", "face", "enter", "exit"}
Z_RANGE = {"prop": (10, 19), "actor": (20, 29), "widget": (30, 39)}
ID_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")

PROMPT_FILE = Path(__file__).resolve().parent.parent / "prompts" / "s7_director.md"
TEMPERATURE = 0.3  # Section 3, LLM calls
MAX_STAGE_ATTEMPTS = 3


class DirectorError(RuntimeError):
    """The director could not produce a valid plan within the allowed attempts. `.report` has the last checks."""

    def __init__(self, message: str, report: ValidationReport):
        super().__init__(message)
        self.report = report


# ================================================================================================= cue params
def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _xy(v) -> bool:
    return isinstance(v, (list, tuple)) and len(v) == 2 and all(_is_num(n) for n in v)


def _camera_to(v) -> str | None:
    if not isinstance(v, dict) or not all(_is_num(v.get(k)) for k in ("x", "y", "zoom")):
        return "needs {x, y, zoom} numbers"
    if not ZOOM[0] <= v["zoom"] <= ZOOM[1]:
        return f"zoom {v['zoom']} outside {ZOOM[0]}-{ZOOM[1]}"
    return None


def _keyframes(frames) -> str | None:
    if not isinstance(frames, list) or not 2 <= len(frames) <= 8:
        return "frames needs 2 to 8 items"
    allowed, last = {"o", "x", "y", "scale", "rotate", "opacity"}, -1.0
    for f in frames:
        if not isinstance(f, dict) or set(f) - allowed or not all(_is_num(v) for v in f.values()):
            return f"bad frame {f!r} (keys o, x, y, scale, rotate, opacity; numbers only)"
        o = f.get("o")
        if o is None or not 0 <= o <= 1 or o <= last:
            return "frame offsets `o` must rise from 0 to 1"
        last = o
        if "opacity" in f and not 0 <= f["opacity"] <= 1:
            return "frame opacity must be 0..1"
    if frames[0]["o"] != 0 or frames[-1]["o"] != 1:
        return "the first frame must have o=0 and the last o=1"
    return None


def check_cue_params(action: str, params: dict) -> str | None:
    """None when the params are valid for the action (Section 7.4), otherwise a short reason."""
    p = dict(params or {})
    if "easing" in p and p.pop("easing") not in EASINGS:
        return f"easing must be one of {sorted(EASINGS)}"
    if action in ("show", "hide"):
        return None if not p else f"{action} takes no params"
    if action == "fade":
        return None if _is_num(p.get("to")) and 0 <= p["to"] <= 1 else "fade needs `to` between 0 and 1"
    if action == "move":
        if not _xy(p.get("to")) or ("from" in p and not _xy(p["from"])):
            return "move needs `to` [dx, dy] (and optional `from` [dx, dy])"
        return None
    if action == "scale":
        ok = _is_num(p.get("to")) and 0.1 <= p["to"] <= 4 and ("from" not in p or _is_num(p["from"]))
        return None if ok else "scale needs `to` between 0.1 and 4"
    if action == "rotate":
        return None if _is_num(p.get("to")) and ("from" not in p or _is_num(p["from"])) else "rotate needs `to` degrees"
    if action == "keyframes":
        return _keyframes(p.get("frames"))
    if action == "highlight":
        n = p.get("pulses", 1)
        return None if isinstance(n, int) and 1 <= n <= 3 else "highlight `pulses` must be 1, 2 or 3"
    if action == "shake":
        a = p.get("amplitude", 8)
        return None if _is_num(a) and 0 < a <= 12 else "shake `amplitude` must be at most 12 px"
    if action == "camera":
        err = _camera_to(p.get("to"))
        if err is None and "from" in p:
            err = _camera_to(p["from"])
        return None if err is None else f"camera {err}"
    if action == "enter":
        return None if p.get("from") in SIDES else "enter needs `from`: left or right"
    if action == "exit":
        return None if p.get("to") in SIDES else "exit needs `to`: left or right"
    if action == "pose":
        return None if p.get("name") in POSES else f"unknown pose {p.get('name')!r}"
    if action == "expression":
        return None if p.get("name") in EXPRESSIONS else f"unknown expression {p.get('name')!r}"
    if action == "face":
        return None if p.get("dir") in SIDES else "face needs `dir`: left or right"
    return f"unknown action {action!r}"


# ================================================================================================= geometry
def _area(b) -> float:
    return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])


def overlap_ratio(a, b) -> float:
    """Intersection area divided by the smaller box's area (rule 6)."""
    inter = [max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])]
    small = min(_area(a), _area(b))
    return _area(inter) / small if small else 0.0


def element_boxes(plan: ScenePlan, catalog: dict, cast: Cast | None) -> dict[str, list[float]]:
    """Stage bounding boxes of every placed element at its planned position, from the catalog boxes times scale.
    Unknown refs and invalid widget params are skipped here (other checks report them)."""
    idx, boxes = index(catalog), {}
    for a in plan.actors:
        cc = cast.character(a.char_key) if cast else None
        boxes[a.char_key] = actor_stage_bbox(cc.build if cc else "child", a.x, a.y, a.scale)
    for p in plan.props:
        info = {"bbox": CUSTOM_BBOX} if custom_ref(p.ref) else idx["prop"].get(p.ref)
        if info:
            boxes[p.id] = prop_stage_bbox(info, p.x, p.y, p.scale)
    for w in plan.widgets:
        try:
            _, lb = render_widget(w.ref, w.params)
        except ValueError:
            continue
        boxes[w.id] = widget_stage_bbox(lb, w.x, w.y, w.scale)
    return boxes


# ================================================================================================= timing
def _timed_cues(plan: ScenePlan, when: dict) -> list[tuple[int, object]]:
    """(scene time, cue) for every cue whose anchor resolves, in time order."""
    out = []
    for c in plan.cues:
        with suppress(UnresolvedCue):
            out.append((resolve_cue(c, when), c))
    return sorted(out, key=lambda tc: (tc[0], tc[1].id))


def change_times(plan: ScenePlan, timed: list) -> list[int]:
    """Times of the cues that make a VISIBLE change (rule 3). A state cue that sets the value an actor already has
    (pose `idle` on an idle actor) changes nothing and does not count; idle loops are not cues at all."""
    state = {a.char_key: {"pose": a.pose, "expression": a.expression, "face": a.facing} for a in plan.actors}
    out = []
    for t, c in timed:
        if c.action in STATE_ACTIONS:
            key = "dir" if c.action == "face" else "name"
            new, cur = c.params.get(key), state.get(c.target, {})
            if cur.get(c.action) == new:
                continue
            cur[c.action] = new
        elif c.action == "enter":
            state.get(c.target, {})["pose"] = "idle"  # enter ends in idle (Section 7.4)
        out.append(t)
    return out


def longest_gap(times: list[int], scene_end: int) -> tuple[int, int, int]:
    """(gap_ms, from_ms, to_ms) of the longest stretch with no change, counting from the scene start to its end."""
    points = [0] + sorted(times) + [scene_end]
    best = (0, 0, 0)
    for a, b in zip(points, points[1:]):
        if b - a > best[0]:
            best = (b - a, a, b)
    return best


def presence(plan: ScenePlan, timed: list) -> dict[str, list[tuple[int, int]]]:
    """On-stage intervals per actor. An actor whose first enter/exit cue is an `enter` starts offstage."""
    out = {}
    for a in plan.actors:
        moves = [(t, c.action) for t, c in timed if c.target == a.char_key and c.action in ("enter", "exit")]
        on = not moves or moves[0][1] != "enter"
        since, spans = 0, []
        for t, action in moves:
            if action == "enter" and not on:
                on, since = True, t
            elif action == "exit" and on:
                spans.append((since, t))
                on = False
        if on:
            spans.append((since, 10**9))
        out[a.char_key] = spans
    return out


def _facing_at(plan: ScenePlan, timed: list, key: str, t: int) -> str:
    facing = next((a.facing for a in plan.actors if a.char_key == key), "right")
    for ct, c in timed:
        if ct > t:
            break
        if c.target == key and c.action == "face":
            facing = c.params.get("dir", facing)
    return facing


# ================================================================================================= validation
def validate_s7(
    plan, *, scene: Scene, audio: AudioOut, catalog: dict, setting: Setting | None = None, cast: Cast | None = None
) -> ValidationReport:
    """Check one scene plan with code. `plan` may be a ScenePlan or the raw dict from the LLM (schema errors such as
    an unknown action are reported as blockers instead of raised)."""
    rep = ValidationReport(stage="S7", scene_id=scene.id)
    if not isinstance(plan, ScenePlan):
        try:
            plan = ScenePlan.model_validate(plan)
        except ValidationError as e:
            msgs = [f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors()[:6]]
            rep.add(
                "schema",
                False,
                "blocker",
                "; ".join(msgs),
                code="SCHEMA",
                fix_hint="return exactly the scene_plan schema; actions only from the cue vocabulary",
            )
            return rep
    rep.add("schema", True, "blocker")
    rep.add(
        "scene_id",
        plan.scene_id == scene.id,
        "blocker",
        f"plan is for {plan.scene_id}, scene is {scene.id}",
        code="SCENE_ID",
    )

    when, scene_end = scene_clock(scene, audio)
    timed = _timed_cues(plan, when)
    _check_ids(rep, plan)
    _check_refs(rep, plan, catalog, setting, cast)
    _check_counts(rep, plan)
    _check_bounds(rep, plan, catalog, cast, timed)
    _check_tween_overlap(rep, plan, timed)
    _check_cues(rep, plan, when)
    _check_changes(rep, plan, timed, scene_end)
    _check_camera(rep, plan)
    _check_widget_text(rep, plan)
    _check_speakers(rep, plan, scene, timed, when)
    _check_bindings(rep, plan, scene, catalog, cast)
    return rep


def _check_ids(rep: ValidationReport, plan: ScenePlan) -> None:
    ids = plan.element_ids()
    dup = sorted({i for i in ids if ids.count(i) > 1})
    rep.add(
        "element_ids_unique",
        not dup and "camera" not in ids,
        "blocker",
        f"duplicate ids {dup}" if dup else "`camera` is reserved",
        code="ID_DUPLICATE",
    )
    bad = [i for i in ids if not i or not i[0].isalpha() or set(i) - ID_CHARS or len(i) > 40]
    rep.add("element_ids_safe", not bad, "blocker", f"ids must be letters, digits, _ or -: {bad}", code="ID_BAD")
    cue_ids = [c.id for c in plan.cues]
    dup = sorted({i for i in cue_ids if cue_ids.count(i) > 1})
    rep.add("cue_ids_unique", not dup, "blocker", f"duplicate cue ids {dup}", code="CUE_ID_DUPLICATE")


def _check_refs(rep, plan, catalog, setting, cast) -> None:
    idx = index(catalog)
    bg = idx["bg"].get(plan.background.id)
    rep.add(
        "background_ref",
        bg is not None,
        "blocker",
        f"unknown background {plan.background.id!r}",
        code="UNKNOWN_REF",
        fix_hint="use the background_id from the setting",
    )
    if bg is not None:
        rep.add(
            "background_time",
            plan.background.time_of_day in bg["supports"],
            "blocker",
            f"{bg['id']} has no {plan.background.time_of_day} version (has {bg['supports']})",
            code="BG_TIME",
        )
    if setting is not None:
        rep.add(
            "background_matches_setting",
            plan.background.id == setting.background_id,
            "major",
            f"setting asks for {setting.background_id}, plan uses {plan.background.id}",
            code="BG_SETTING",
        )
    custom_keys = {c.key for c in setting.custom_props} if setting else None
    for p in plan.props:
        key = custom_ref(p.ref)
        if key is not None:
            ok = custom_keys is None or key in custom_keys
            rep.add(
                f"prop_ref_{p.id}",
                ok,
                "blocker",
                f"custom prop {key!r} is not in the setting",
                code="UNKNOWN_REF",
                fix_hint=f"custom refs must be one of {sorted(custom_keys or [])}",
            )
        else:
            rep.add(
                f"prop_ref_{p.id}",
                p.ref in idx["prop"],
                "blocker",
                f"unknown prop ref {p.ref!r}",
                code="UNKNOWN_REF",
                fix_hint="use only prop ids from the catalog",
            )
    for w in plan.widgets:
        if not rep.add(
            f"widget_ref_{w.id}",
            w.ref in WIDGETS,
            "blocker",
            f"unknown widget ref {w.ref!r}",
            code="UNKNOWN_REF",
            fix_hint="use only widget ids from the catalog",
        ):
            continue
        try:
            render_widget(w.ref, w.params)
            problem = ""
        except ValueError as e:
            problem = str(e)
        rep.add(f"widget_params_{w.id}", not problem, "blocker", problem, code="WIDGET_PARAMS")
    for a in plan.actors:
        if cast is not None:
            rep.add(
                f"actor_in_cast_{a.char_key}",
                cast.character(a.char_key) is not None,
                "blocker",
                f"{a.char_key} is not in the cast",
                code="UNKNOWN_ACTOR",
            )
        rep.add(
            f"actor_pose_{a.char_key}",
            a.pose in POSES and a.expression in EXPRESSIONS,
            "blocker",
            f"{a.char_key}: pose {a.pose!r} / expression {a.expression!r} not in the catalog",
            code="ACTOR_STATE",
        )
    for kind, items in (("prop", plan.props), ("actor", plan.actors), ("widget", plan.widgets)):
        lo, hi = Z_RANGE[kind]
        off = [getattr(i, "id", None) or i.char_key for i in items if not lo <= i.z <= hi]
        rep.add(f"z_{kind}s", not off, "minor", f"{kind} z outside {lo}-{hi}: {off}", code="Z_ORDER")


def _check_counts(rep, plan) -> None:
    keys = [a.char_key for a in plan.actors]
    rep.add(
        "actor_count",
        len(plan.actors) <= MAX_ACTORS,
        "blocker",
        f"{len(plan.actors)} actors (max {MAX_ACTORS})",
        code="TOO_MANY_ACTORS",
    )
    rep.add("actors_unique", len(set(keys)) == len(keys), "blocker", "an actor is placed twice", code="ACTOR_TWICE")
    n = len(plan.props) + len(plan.widgets)
    rep.add(
        "element_count",
        n <= MAX_ELEMENTS,
        "blocker",
        f"{n} props + widgets (max {MAX_ELEMENTS})",
        code="TOO_MANY_ELEMENTS",
    )
    rep.add(
        "cue_count",
        len(plan.cues) <= MAX_CUES,
        "blocker",
        f"{len(plan.cues)} cues (max {MAX_CUES})",
        code="TOO_MANY_CUES",
    )


def _inside(x, y) -> bool:
    return SAFE_X[0] <= x <= SAFE_X[1] and SAFE_Y[0] <= y <= SAFE_Y[1]


def _check_bounds(rep, plan, catalog, cast, timed) -> None:
    hint = f"keep x within {SAFE_X[0]}-{SAFE_X[1]} and y within {SAFE_Y[0]}-{SAFE_Y[1]}"
    walks = {c.target for _, c in timed if c.action in ("enter", "exit")}
    for a in plan.actors:
        if a.offstage_ok:
            # offstage_ok only relaxes the 40 px margin, and only for an actor who walks in or out: the planned
            # position is where the actor stands while on stage, so it must still be on the stage
            rep.add(
                f"offstage_ok_{a.char_key}",
                a.char_key in walks,
                "blocker",
                f"{a.char_key} has offstage_ok but no enter or exit cue",
                code="OFFSTAGE_NO_CUE",
                fix_hint="add an enter or exit cue for this actor, or set offstage_ok to false",
            )
            ok = 0 <= a.x <= STAGE_W and 0 <= a.y <= STAGE_H
        else:
            ok = _inside(a.x, a.y)
        rep.add(
            f"bounds_{a.char_key}",
            ok,
            "blocker",
            f"{a.char_key} at ({a.x:g}, {a.y:g}) is outside the safe area",
            code="OUT_OF_BOUNDS",
            fix_hint=hint,
        )
    for p in plan.props:
        rep.add(
            f"bounds_{p.id}",
            _inside(p.x, p.y),
            "blocker",
            f"{p.id} at ({p.x:g}, {p.y:g}) is outside the safe area",
            code="OUT_OF_BOUNDS",
            fix_hint=hint,
        )
    boxes = element_boxes(plan, catalog, cast)
    for w in plan.widgets:
        b = boxes.get(w.id)
        ok = b is not None and b[0] >= SAFE_X[0] and b[2] <= SAFE_X[1] and b[1] >= SAFE_Y[0] and b[3] <= SAFE_Y[1]
        if b is not None:
            rep.add(
                f"bounds_{w.id}",
                ok,
                "blocker",
                f"widget {w.id} box {[round(v) for v in b]} leaves the safe area",
                code="OUT_OF_BOUNDS",
                fix_hint=hint + " for the whole widget box (lower its scale or move it)",
            )
    _check_move_targets(rep, plan, hint, boxes)
    spans = presence(plan, timed)
    for i, a in enumerate(plan.actors):
        for b in plan.actors[i + 1 :]:
            together = any(s1 < e2 and s2 < e1 for s1, e1 in spans[a.char_key] for s2, e2 in spans[b.char_key])
            r = overlap_ratio(boxes[a.char_key], boxes[b.char_key]) if together else 0.0
            rep.add(
                f"overlap_{a.char_key}_{b.char_key}",
                r <= MAX_OVERLAP,
                "blocker",
                f"{a.char_key} and {b.char_key} overlap {r:.0%} (max {MAX_OVERLAP:.0%})",
                code="ACTOR_OVERLAP",
                fix_hint="keep children about 220 px apart and adults about 240 px apart",
            )


def _check_move_targets(rep, plan, hint, boxes=None) -> None:
    """A move or keyframes cue is relative to the planned position: where it starts (`from`) and ends must still be on
    the stage. For props and widgets the whole moved box must stay on the stage, as S11 checks it in the browser."""
    anchors = {a.char_key: (a.x, a.y) for a in plan.actors}
    anchors |= {p.id: (p.x, p.y) for p in plan.props} | {w.id: (w.x, w.y) for w in plan.widgets}
    things = {p.id for p in plan.props} | {w.id for w in plan.widgets}
    boxes = boxes or {}
    for c in plan.cues:
        if c.target not in anchors or c.action not in ("move", "keyframes"):
            continue
        x0, y0 = anchors[c.target]
        if c.action == "move":
            offsets = [c.params.get("to")] + ([c.params["from"]] if "from" in c.params else [])
        else:
            offsets = [(f.get("x", 0), f.get("y", 0)) for f in c.params.get("frames") or [] if isinstance(f, dict)]
        offsets = [o for o in offsets if _xy(o)]
        bad = [o for o in offsets if not _inside(x0 + o[0], y0 + o[1])]
        message = f"{c.id} moves {c.target} to ({x0 + bad[0][0]:g}, {y0 + bad[0][1]:g}), off the stage" if bad else ""
        box = boxes.get(c.target) if c.target in things else None
        if box is not None and not bad:
            for o in offsets:
                moved = [box[0] + o[0], box[1] + o[1], box[2] + o[0], box[3] + o[1]]
                if moved[0] < 0 or moved[1] < 0 or moved[2] > STAGE_W or moved[3] > STAGE_H:
                    bad.append(o)
                    message = f"{c.id} moves {c.target}'s box to {[round(v) for v in moved]}, past the stage edge"
                    break
        rep.add(
            f"move_bounds_{c.id}",
            not bad,
            "blocker",
            message,
            code="OUT_OF_BOUNDS",
            fix_hint=hint + " at the start and the end of every move (from and to are relative to the planned "
            "position), and keep a prop's whole box on the stage",
        )


def _check_cues(rep, plan, when) -> None:
    actors = {a.char_key for a in plan.actors}
    targets = actors | {p.id for p in plan.props} | {w.id for w in plan.widgets}
    for c in plan.cues:
        if c.action == "camera":
            ok, why = c.target == "camera", "a camera cue must target `camera`"
        elif c.action in ACTOR_ONLY:
            ok, why = c.target in actors, f"{c.action} needs an actor target, got {c.target!r}"
        else:
            ok, why = c.target in targets, f"target {c.target!r} does not exist"
        rep.add(
            f"cue_target_{c.id}",
            ok,
            "blocker",
            f"{c.id}: {why}",
            code="CUE_TARGET",
            fix_hint="target an actor key, a prop or widget id from this plan, or `camera`",
        )
        err = check_cue_params(c.action, c.params)
        rep.add(f"cue_params_{c.id}", err is None, "blocker", f"{c.id}: {err}", code="CUE_PARAMS")
        rep.add(
            f"cue_dur_{c.id}", 0 <= c.dur_ms <= MAX_DUR_MS, "blocker", f"{c.id}: dur_ms {c.dur_ms}", code="CUE_PARAMS"
        )
        try:
            resolve_cue(c, when)
            ok, why = True, ""
        except UnresolvedCue as e:
            ok, why = False, str(e)
        rep.add(
            f"cue_anchor_{c.id}",
            ok,
            "blocker",
            f"{c.id}: {why}",
            code="CUE_ANCHOR",
            line_id=c.at.line,
            fix_hint="anchor on a line of this scene; anchor `word` needs a word_index inside that line",
        )


def _check_changes(rep, plan, timed, scene_end) -> None:
    gap, a, b = longest_gap(change_times(plan, timed), scene_end)
    rep.add(
        "visual_change_6s",
        gap <= CHANGE_EVERY_MS,
        "blocker",
        f"no visible change for {gap / 1000:.1f} s (scene time {a / 1000:.1f}-{b / 1000:.1f} s)",
        code="NO_VISUAL_CHANGE",
        fix_hint="add a pose, expression, highlight or show cue inside that stretch (anchor it on a word)",
    )


def _check_camera(rep, plan) -> None:
    moves = [c for c in plan.cues if c.action == "camera"]
    rep.add(
        "camera_moves",
        len(moves) <= MAX_CAMERA_MOVES,
        "blocker",
        f"{len(moves)} camera moves (max {MAX_CAMERA_MOVES})",
        code="CAMERA_MOVES",
    )
    rep.add(
        "camera_zoom",
        ZOOM[0] <= plan.camera.zoom <= ZOOM[1],
        "blocker",
        f"camera zoom {plan.camera.zoom} outside {ZOOM[0]}-{ZOOM[1]}",
        code="CAMERA_ZOOM",
    )
    views = [("camera", plan.camera.x, plan.camera.y, plan.camera.zoom)]
    for c in moves:
        for key in ("to", "from"):
            v = c.params.get(key)
            if isinstance(v, dict) and all(_is_num(v.get(k)) for k in ("x", "y", "zoom")):
                views.append((c.id, v["x"], v["y"], v["zoom"]))
    for name, x, y, z in views:
        if not ZOOM[0] <= z <= ZOOM[1]:
            continue  # reported by camera_zoom / cue params
        hw, hh = STAGE_W / 2 / z, STAGE_H / 2 / z
        ok = hw - 0.5 <= x <= STAGE_W - hw + 0.5 and hh - 0.5 <= y <= STAGE_H - hh + 0.5
        rep.add(
            f"camera_bounds_{name}",
            ok,
            "blocker",
            f"{name}: the camera at ({x:g}, {y:g}) zoom {z:g} shows outside the stage",
            code="CAMERA_BOUNDS",
            fix_hint=f"at zoom {z:g} keep x in {hw:.0f}-{STAGE_W - hw:.0f} and y in {hh:.0f}-{STAGE_H - hh:.0f}",
        )


TRANSFORM_TWEENS = {"move", "scale", "rotate", "keyframes", "highlight", "shake", "enter", "exit", "camera"}
OPACITY_TWEENS = {"show", "hide", "fade", "keyframes"}


def _check_tween_overlap(rep, plan, timed) -> None:
    """Two tweens that animate the same property of the same element at the same time fight: the later one replaces
    the earlier one and the element jumps. Transform tweens and opacity tweens are checked separately."""
    for kind, actions in (("transform", TRANSFORM_TWEENS), ("opacity", OPACITY_TWEENS)):
        last_end: dict[str, tuple[int, str]] = {}
        for t, c in timed:
            if c.action not in actions or c.dur_ms <= 0:
                continue
            prev = last_end.get(c.target)
            rep.add(
                f"tween_overlap_{kind}_{c.id}",
                prev is None or t >= prev[0],
                "blocker",
                f"{c.id} starts while {prev[1]} still animates the {kind} of {c.target}" if prev else "",
                code="TWEEN_OVERLAP",
                fix_hint=f"start {c.id} after {prev[1]} ends, or shorten {prev[1]}" if prev else "",
            )
            if prev is None or t + c.dur_ms > prev[0]:
                last_end[c.target] = (t + c.dur_ms, c.id)


def _check_widget_text(rep, plan) -> None:
    for w in plan.widgets:
        try:
            markup, _ = render_widget(w.ref, w.params)
        except ValueError:
            continue  # already reported by widget_params
        problems = []
        for text, size in widget_texts(markup):
            words, px = len(tokens(text)), size * w.scale
            if words > MAX_LABEL_WORDS:
                problems.append(f"{text!r} has {words} words")
            if px < MIN_TEXT - 0.5:
                problems.append(f"{text!r} is {px:.0f} px")
        rep.add(
            f"widget_text_{w.id}",
            not problems,
            "blocker",
            f"{w.id}: {'; '.join(problems)} (labels: at most {MAX_LABEL_WORDS} words, at least {MIN_TEXT} px)",
            code="WIDGET_TEXT",
            fix_hint="shorten the label or raise the widget scale",
        )


def _check_speakers(rep, plan, scene, timed, when) -> None:
    spans = presence(plan, timed)
    xs = {a.char_key: a.x for a in plan.actors}
    for line in scene.lines:
        if line.speaker == "narrator":
            continue
        start = when[line.id][0]
        on = any(a <= start < b for a, b in spans.get(line.speaker, []))
        rep.add(
            f"speaker_on_stage_{line.id}",
            on,
            "blocker",
            f"{line.speaker} speaks {line.id} but is not on stage",
            code="SPEAKER_OFFSTAGE",
            line_id=line.id,
            fix_hint=f"place {line.speaker} as an actor, or add an `enter` cue before {line.id}",
        )
        if not on:
            continue
        others = [k for k, s in spans.items() if k != line.speaker and any(a <= start < b for a, b in s)]
        if others:
            facing = _facing_at(plan, timed, line.speaker, start)
            sign = 1 if facing == "right" else -1
            faces = any((xs[o] - xs[line.speaker]) * sign > 0 for o in others)
            rep.add(
                f"speaker_faces_{line.id}",
                faces,
                "minor",
                f"{line.speaker} faces {facing} away from everyone during {line.id}",
                code="SPEAKER_FACING",
                line_id=line.id,
            )


def _check_bindings(rep, plan, scene, catalog, cast) -> None:
    it = scene.interaction
    tap = it if it is not None and it.type == "tap_reveal" else None
    stray = [b.interaction_id for b in plan.interaction_bindings if tap is None or b.interaction_id != tap.id]
    if it is None:  # spec 06 S7: bindings exist only for tap_reveal; every other scene has "interaction_bindings": []
        why = "this scene has no interaction"
    elif tap is None:
        why = f"interaction {it.id} is {it.type}, and only tap_reveal interactions take bindings"
    else:
        why = f"the only interaction to bind here is {tap.id}"
    rep.add(
        "bindings_known",
        not stray,
        "blocker",
        f"interaction_bindings for {stray}: {why}",
        code="BINDING",
        fix_hint='write "interaction_bindings": []' if tap is None else f"keep only the binding for {tap.id}",
    )
    if tap is None:
        return
    binding = next((b for b in plan.interaction_bindings if b.interaction_id == tap.id), None)
    if not rep.add(
        "bindings_present",
        binding is not None,
        "blocker",
        f"no interaction_bindings for {tap.id}",
        code="BINDING",
        fix_hint="bind each tap_reveal target's thing to a prop or widget id",
    ):
        return
    indexes = sorted(t.index for t in binding.targets)
    rep.add(
        "bindings_complete",
        indexes == list(range(len(tap.targets))),
        "blocker",
        f"{tap.id} has {len(tap.targets)} targets, bound indexes are {indexes}",
        code="BINDING",
    )
    boxes = element_boxes(plan, catalog, cast)
    tappable = {p.id for p in plan.props} | {w.id for w in plan.widgets}
    for t in binding.targets:
        b = boxes.get(t.element_id) if t.element_id in tappable else None
        if not rep.add(
            f"binding_target_{t.index}",
            b is not None,
            "blocker",
            f"target {t.index} is bound to {t.element_id!r}, which is not a placed prop or widget",
            code="BINDING",
        ):
            continue
        w, h = b[2] - b[0], b[3] - b[1]
        rep.add(
            f"binding_size_{t.index}",
            w >= MIN_TAP_PX and h >= MIN_TAP_PX,
            "blocker",
            f"{t.element_id} is {w:.0f}x{h:.0f} px (tap targets need {MIN_TAP_PX}x{MIN_TAP_PX})",
            code="TAP_TARGET_SMALL",
            fix_hint="raise its scale or bind a bigger element",
        )


# ================================================================================================= LLM call
PLAN_SCHEMA = ScenePlan.model_json_schema()


def director_input(
    scene: Scene,
    audio: AudioOut,
    cast: Cast,
    setting: Setting,
    catalog: dict,
    feedback: list[str] | None = None,
    previous_plan: ScenePlan | dict | None = None,
) -> str:
    """The user message: the scene with real timings, the cast present, the setting and the catalog; when a plan is
    being changed (a failed check, an S11 issue, a reviewer's comment), the current plan and what to change."""
    lines = []
    for line in scene.lines:
        a = audio.line(line.id)
        lines.append(
            {
                "id": line.id,
                "speaker": line.speaker,
                "emotion": line.emotion,
                "text": line.text_display,
                "duration_ms": a.duration_ms if a else None,
                "words": [[i, w.w, w.s] for i, w in enumerate(a.words)] if a else [],
            }
        )
    speakers = {line.speaker for line in scene.lines} - {"narrator"}
    present = [c.model_dump(include={"key", "build", "presentation"}) for c in cast.characters if c.key in speakers]
    scene_data = {
        "scene_id": scene.id,
        "summary": scene.summary,
        "lines": lines,
        "interaction": scene.interaction.model_dump() if scene.interaction else None,
    }
    parts = [
        "<scene>\n" + _json_block(scene_data) + "\n</scene>",
        "Each word is [word_index, word, start_ms inside the line].",
        "<cast_present>\n" + _json_block(present) + "\n</cast_present>",
        "<setting>\n" + _json_block(setting.model_dump()) + "\n</setting>",
        "<catalog>\n" + _json_block(_catalog_for_prompt(catalog, setting)) + "\n</catalog>",
    ]
    if previous_plan is not None:
        plan = previous_plan.model_dump() if isinstance(previous_plan, ScenePlan) else previous_plan
        parts.append(
            "<current_plan>\n" + _json_block(plan) + "\n</current_plan>\n"
            "Change only what the points below need; keep the rest of the current plan."
        )
    if feedback:
        parts.append("Fix every one of these points:\n- " + "\n- ".join(feedback))
    return "\n\n".join(parts)


def _json_block(obj) -> str:
    """JSON for a <tag> block of the prompt; "</" is written "<\\/" so text inside can never close the tag."""
    return json.dumps(obj, ensure_ascii=False).replace("</", "<\\/")


def _catalog_for_prompt(catalog: dict, setting: Setting) -> dict:
    """The parts of the catalog the director needs: the setting's background, all props and widgets, poses."""
    idx = index(catalog)
    return {
        "conventions": catalog["conventions"],
        "background": idx["bg"].get(setting.background_id),
        "props": catalog["props"],
        "widgets": catalog["widgets"],
        "poses": catalog["poses"],
        "expressions": catalog["expressions"],
        "character_bbox": catalog["character_bbox"],
    }


def _feedback(rep: ValidationReport) -> list[str]:
    return [
        f"[{i.code}] {i.message}" + (f" (fix: {i.fix_hint})" if i.fix_hint else "")
        for i in rep.issues
        if i.severity in ("blocker", "major")
    ]


def run(
    scene: Scene,
    audio: AudioOut,
    cast: Cast,
    catalog: dict,
    llm,
    *,
    feedback: list[str] | None = None,
    previous_plan: ScenePlan | None = None,
    attempts: int = MAX_STAGE_ATTEMPTS,
) -> tuple[ScenePlan, ValidationReport]:
    """Ask the director for a plan until it passes `validate_s7`, appending the failed checks each time
    (Section 3, Retry). `previous_plan` + `feedback` change an existing plan (S11 repair, revisions).
    Raises DirectorError when attempts run out."""
    setting = cast.setting(scene.id)
    if setting is None:
        raise ValueError(f"no setting for scene {scene.id}")
    system = PROMPT_FILE.read_text(encoding="utf-8")
    rep = None
    for _ in range(attempts):
        raw = llm.complete_json(
            system,
            director_input(scene, audio, cast, setting, catalog, feedback, previous_plan),
            PLAN_SCHEMA,
            "large",
            TEMPERATURE,
            8000,
        )
        rep = validate_s7(raw, scene=scene, audio=audio, catalog=catalog, setting=setting, cast=cast)
        if rep.passed:
            return ScenePlan.model_validate(raw), rep
        feedback, previous_plan = _feedback(rep), raw
    raise DirectorError(f"S7 {scene.id}: no valid plan after {attempts} attempts", rep)
