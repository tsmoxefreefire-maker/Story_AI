"""The revision run (Section 10.2): open comments -> one new version that changes only what the comments need.

1. Every open comment becomes `batched`; the Revision Planner turns them into patches or rejects them.
   All rejected -> no new version.
2. A new version (`trigger = revision`, parent = the published version, else the latest) starts from a copy of the
   parent's build: untouched scenes, their audio and plans are reused as they are.
3. Each patch runs only what it needs:
     visual       S7 for the scene with the instruction as feedback, then S8
     audio        S6 for the listed lines
     lines        S4 in edit mode (module 4, `edit_lines`), S6 for the changed lines, then S7 and S8 for the scene
     interaction  S4 edit for the interaction, then S7 (new bindings) and S8
     cast         S5 again (module 5, `rerun_cast`), then S7 and S8 for every scene
   then S9 and S10 once.
4. S11 for the touched scenes plus the bundle checks. If lines changed, the Story Critic's coverage check runs over
   the whole script (`coverage_check`, module 4).
5. Everything passes -> S12 publishes (or `needs_review` with REVISION_REQUIRES_APPROVAL); comments are `applied`.
   Otherwise the version is `needs_review` and the previous published version stays published."""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass, field

from ...providers.blobstore import version_prefix
from ..models import Script
from ..module6 import StageFailed, StoryBuild
from ..stages import s12_publish
from ..stages.s7_director import DirectorError
from ..stages.s11_repair import served
from ..stages.s11_validate import S11Result
from ..stages.s11_validate import run as validate_bundle
from ..versions import StoryVersion, VersionStore, ensure_not_busy, next_version, utcnow
from .comments import BATCH_WINDOW_SEC, take_open
from .planner import Patch, PlannerError, plan_revision

EditLines = Callable[[Script, str, str, list[str]], Script]  # (script, scene_id, instruction, line ids) -> script


@dataclass
class RevisionOutcome:
    status: str  # "published", "needs_review", "rejected" (no new version) or "nothing" (no open comment)
    version: StoryVersion | None = None
    build: StoryBuild | None = None
    touched: set[str] = field(default_factory=set)
    log: list[str] = field(default_factory=list)


def _issue(code: str, message: str, scene_id: str | None = None, severity: str = "major") -> dict:
    return {
        "severity": severity,
        "code": code,
        "message": message,
        "fix_hint": "",
        "suspect_stage": None,
        "scene_id": scene_id,
        "line_id": None,
    }


def run(
    store: VersionStore,
    story_id: str,
    parent: StoryBuild,
    *,
    planner_llm,
    concepts=None,
    edit_lines: EditLines | None = None,
    edit_interaction: EditLines | None = None,
    rerun_cast: Callable[[StoryBuild, list[str]], None] | None = None,
    coverage_check: Callable[[Script], list[str]] | None = None,
    validate: Callable[[StoryBuild, set[str]], S11Result] | None = None,
    requires_approval: bool = False,
    vlm=None,
) -> RevisionOutcome:
    ensure_not_busy(store, story_id)  # a version is running: the job waits, the comments stay open (10 table)
    comments = take_open(store, story_id)
    if not comments:
        return RevisionOutcome("nothing")
    try:
        plan = plan_revision(planner_llm, comments, parent.script, parent.plans, parent.cast, concepts)
    except PlannerError as e:
        _reopen(store, story_id, comments)
        return RevisionOutcome("needs_review", log=[f"planner failed: {e}"])
    for c in comments:
        if c.id in plan.rejected:
            c.status, c.reason = "rejected", plan.rejected[c.id]
            store.save_comment(c)
    if not plan.patches:
        return RevisionOutcome("rejected", log=["every comment was rejected"])

    story = store.get_story(story_id)
    versions = store.versions(story_id)
    parent_n = story.published_version or (versions[-1].version if versions else None)
    v = next_version(store, story_id, "revision", parent=parent_n)
    v.status, v.started_at = "running", utcnow()
    if parent_n is not None:
        v.idea = store.get_version(story_id, parent_n).idea
    v.result["comment_ids"] = [c.id for c in comments if c.status == "batched"]
    store.save_version(v)

    build = parent.fork(prefix=version_prefix(story_id, v.version) if parent.blob is not None else None)
    log: list[str] = []
    touched: set[str] = set()
    lines_changed = False
    try:
        for p in plan.patches:
            touched |= _apply(build, p, parent, edit_lines, edit_interaction, rerun_cast, log)
            lines_changed |= p.scope == "lines"
        build.assemble()
    except (StageFailed, DirectorError, _CannotRun) as e:
        # the stages ran out of attempts: a blocker, so nobody can approve a version that has no finished bundle
        v.issues.append(_issue("VALIDATION_EXHAUSTED", str(e), severity="blocker"))
        return _finish_failed(store, v, build, touched, log + [f"stopped: {e}"])
    except Exception as e:  # noqa: BLE001  a defect: the version fails, the story is never left busy
        return _fail_internal(store, v, comments, build, touched, log + [f"internal error: {e!r}"])

    try:
        result = (validate or _default_validate(vlm))(build, touched)
    except Exception as e:  # noqa: BLE001
        return _fail_internal(store, v, comments, build, touched, log + [f"validation error: {e!r}"])
    v.issues = [i.model_dump() for i in result.issues]
    if lines_changed:
        if coverage_check is None:
            v.issues.append(
                _issue(
                    "COVERAGE_UNCHECKED",
                    "lines changed but the Story Critic's coverage check is not connected (module 4)",
                    severity="minor",
                )
            )
        else:
            v.issues += [_issue("COVERAGE", m) for m in coverage_check(build.script)]
    v.result.update(build.bundle.meta.model_dump())
    v.result["warnings"] = [w.model_dump() for w in build.warnings]
    store.save_version(v)
    if v.blocking() or requires_approval:
        return _finish_failed(store, v, build, touched, log)
    s12_publish.run(store, story_id, v.version)  # also marks the version's comments applied
    return RevisionOutcome("published", store.get_version(story_id, v.version), build, touched, log)


def _reopen(store: VersionStore, story_id: str, comments, window_sec: int = BATCH_WINDOW_SEC) -> None:
    """Back to open, and a new revision job is scheduled so they are not forgotten."""
    for c in comments:
        if c.status == "batched":
            c.status = "open"
            store.save_comment(c)
    store.set_revision_due(story_id, utcnow() + dt.timedelta(seconds=window_sec))


def _fail_internal(store, v: StoryVersion, comments, build, touched, log) -> RevisionOutcome:
    v.issues.append(_issue("INTERNAL", log[-1], severity="blocker"))
    v.status, v.finished_at = "failed", utcnow()
    store.save_version(v)
    _reopen(store, v.story_id, comments)
    from ...core import webhooks

    webhooks.enqueue(store, store.get_story(v.story_id), v.version, "story.failed", "failed")
    return RevisionOutcome("failed", v, build, touched, log)


class _CannotRun(RuntimeError):
    """A patch needs a stage that is not available here (module 4 or 5)."""


def _changed_lines(old: Script, new: Script) -> set[str]:
    before = {ln.id: ln.text_spoken for ln in old.all_lines()}
    return {ln.id for ln in new.all_lines() if before.get(ln.id) != ln.text_spoken}


def _apply(
    build: StoryBuild, p: Patch, parent: StoryBuild, edit_lines, edit_interaction, rerun_cast, log: list[str]
) -> set[str]:
    sid = p.scene_id
    if p.scope == "visual":
        build.direct_scene(sid, feedback=[p.instruction])
        build.compose_scene(sid)
        log.append(f"visual {sid}: S7 + S8")
        return {sid}
    if p.scope == "audio":
        build.produce_audio(only_lines=set(p.changed_line_ids))
        log.append(f"audio {sid}: S6 for {p.changed_line_ids}")
        return {sid}
    if p.scope == "lines":
        if edit_lines is None:
            raise _CannotRun("a lines patch needs S4 in edit mode (module 4)")
        build.script = edit_lines(build.script, sid, p.instruction, p.changed_line_ids)
        changed = _changed_lines(parent.script, build.script) | set(p.changed_line_ids)
        build.produce_audio(only_lines=changed)
        build.direct_scene(sid, feedback=[p.instruction])
        build.compose_scene(sid)
        log.append(f"lines {sid}: S4 edit + S6 {sorted(changed)} + S7 + S8")
        return {sid}
    if p.scope == "interaction":
        if edit_interaction is None:
            raise _CannotRun("an interaction patch needs S4 in edit mode (module 4)")
        build.script = edit_interaction(build.script, sid, p.instruction, [])
        build.direct_scene(sid, feedback=[p.instruction])
        build.compose_scene(sid)
        log.append(f"interaction {sid}: S4 edit + S7 bindings + S8")
        return {sid}
    if p.scope == "cast":
        if rerun_cast is None:
            raise _CannotRun("a cast patch needs S5 (module 5)")
        rerun_cast(build, [p.instruction])
        for sc in build.script.scenes:
            build.direct_scene(sc.id, feedback=[p.instruction])
            build.compose_scene(sc.id)
        log.append("cast: S5 + S7/S8 for every scene")
        return {sc.id for sc in build.script.scenes}
    raise _CannotRun(f"unknown scope {p.scope}")


def _default_validate(vlm):
    def validate(build: StoryBuild, touched: set[str]) -> S11Result:
        with served(build) as url:
            return validate_bundle(
                url, vlm=vlm, script=build.script, cast=build.cast, age_group=build.intake.age_group, scenes=touched
            )

    return validate


def _finish_failed(store, v: StoryVersion, build, touched, log) -> RevisionOutcome:
    """The new version waits for a human; the previous published version stays published."""
    from ...core import webhooks

    v.status, v.finished_at = "needs_review", utcnow()
    store.save_version(v)
    webhooks.enqueue(store, store.get_story(v.story_id), v.version, "story.needs_review", "needs_review")
    return RevisionOutcome("needs_review", v, build, touched, log)
