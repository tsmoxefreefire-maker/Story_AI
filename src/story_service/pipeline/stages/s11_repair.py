"""S11 repair loop (Section 5, S11): validate the bundle, repair what failed, validate the touched scenes again.

| suspect_stage   | repair                                                                     |
| S7_director     | S7 again for that scene with the issue's fix_hint, then S8, S9, S10        |
| S8_prop         | draw the scene's custom props again, then S8, S9, S10                      |
| S5_cast         | S5 again with the hint (once per version), then every scene                |
| S10_assembler   | a code defect: the version fails with INTERNAL                             |

After each repair only the touched scenes and the bundle-level checks are validated again. A scene gets at most
MAX_SCENE_REPAIRS (2) repairs; when that is not enough, or an issue points at a stage the loop cannot re-run, the
version ends `needs_review` with the open issues."""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from ...devtools.bundle_server import serving
from ..models import Issue
from ..module6 import StageFailed, StoryBuild
from .s7_director import DirectorError
from .s11_validate import S11Result
from .s11_validate import run as validate_bundle

MAX_SCENE_REPAIRS = 2


@dataclass
class RepairOutcome:
    status: str  # "passed", "needs_review" or "failed"
    result: S11Result
    repairs: dict[str, int] = field(default_factory=dict)
    code: str | None = None  # "INTERNAL" when the bundle code itself is at fault
    log: list[str] = field(default_factory=list)

    @property
    def issues(self) -> list[Issue]:
        return self.result.issues


@contextmanager
def served(build: StoryBuild):
    """Write the current bundle to a temporary folder and serve it with the production headers."""
    with tempfile.TemporaryDirectory(prefix="s11_") as folder:
        for rel, data in build.bundle.files().items():
            path = Path(folder) / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        with serving(folder) as url:
            yield url


def _default_validate(build: StoryBuild, vlm, s11_options: dict) -> Callable[[set[str] | None], S11Result]:
    def validate(scenes: set[str] | None) -> S11Result:
        with served(build) as url:
            return validate_bundle(
                url,
                vlm=vlm,
                script=build.script,
                cast=build.cast,
                age_group=build.intake.age_group,
                scenes=scenes,
                **s11_options,
            )

    return validate


def _merge(old: S11Result, new: S11Result, touched: set[str]) -> S11Result:
    """Keep the reports of untouched scenes, take the touched scenes and the bundle report from the new run."""
    keep = [r for r in old.reports if r.scene_id is not None and r.scene_id not in touched]
    fresh = [r for r in new.reports if r.scene_id is None or r.scene_id in touched]
    order = {r.scene_id: i for i, r in enumerate(old.reports)}
    return S11Result(reports=sorted(keep + fresh, key=lambda r: order.get(r.scene_id, len(order))))


def run(
    build: StoryBuild,
    *,
    vlm=None,
    validate: Callable[[set[str] | None], S11Result] | None = None,
    rerun_cast: Callable[[StoryBuild, list[str]], None] | None = None,
    max_scene_repairs: int = MAX_SCENE_REPAIRS,
    **s11_options,
) -> RepairOutcome:
    """Run S11 on `build.bundle` and repair it in place. `validate(scenes)` can replace the browser validation (tests);
    `rerun_cast(build, hints)` re-runs S5 (module 5) and is used at most once."""
    validate = validate or _default_validate(build, vlm, s11_options)
    result = validate(None)
    repairs: dict[str, int] = {}
    log: list[str] = []
    cast_done = False
    while True:
        open_issues = [i for i in result.issues if i.severity in ("blocker", "major")]
        if not open_issues:
            return RepairOutcome("passed", result, repairs, log=log)
        if any(i.suspect_stage == "S10_assembler" for i in open_issues):
            log.append("S10_assembler issue: a code defect, the version fails")
            return RepairOutcome("failed", result, repairs, code="INTERNAL", log=log)
        touched: set[str] = set()
        try:
            cast_issues = [i for i in open_issues if i.suspect_stage == "S5_cast"]
            if cast_issues:
                if cast_done or rerun_cast is None:
                    log.append("S5_cast issue and S5 cannot run again")
                    return RepairOutcome("needs_review", result, repairs, log=log)
                cast_done = True
                rerun_cast(build, [i.fix_hint or i.message for i in cast_issues])
                for sc in build.script.scenes:
                    build.direct_scene(sc.id, feedback=[i.fix_hint or i.message for i in cast_issues])
                    build.compose_scene(sc.id)
                touched = {sc.id for sc in build.script.scenes}
                log.append("re-ran S5 and every scene")
            else:
                by_scene: dict[str, list[Issue]] = {}
                for i in open_issues:
                    if i.scene_id is None or i.suspect_stage not in ("S7_director", "S8_prop"):
                        log.append(f"{i.code} ({i.suspect_stage}) cannot be repaired here")
                        return RepairOutcome("needs_review", result, repairs, log=log)
                    by_scene.setdefault(i.scene_id, []).append(i)
                for scene_id, issues in by_scene.items():
                    if repairs.get(scene_id, 0) >= max_scene_repairs:
                        log.append(f"{scene_id}: still failing after {max_scene_repairs} repairs")
                        return RepairOutcome("needs_review", result, repairs, log=log)
                    repairs[scene_id] = repairs.get(scene_id, 0) + 1
                    hints = [
                        f"[{i.code}] {i.message}" + (f" (fix: {i.fix_hint})" if i.fix_hint else "")
                        for i in issues
                        if i.suspect_stage == "S7_director"
                    ]
                    if hints:
                        build.direct_scene(scene_id, feedback=hints)
                    build.compose_scene(
                        scene_id, refresh_custom_props=any(i.suspect_stage == "S8_prop" for i in issues)
                    )
                    touched.add(scene_id)
                    log.append(
                        f"{scene_id}: repair {repairs[scene_id]} ({', '.join(sorted({i.code for i in issues}))})"
                    )
            build.assemble()
        except (StageFailed, DirectorError) as e:
            log.append(f"repair stopped: {e}")
            return RepairOutcome("needs_review", result, repairs, log=log)
        result = _merge(result, validate(touched), touched)
