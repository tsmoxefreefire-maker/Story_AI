"""Every example story builds end to end (S6 to S10, test voices) and a one-scene preview skips the length check."""

import shutil
from pathlib import Path

import pytest

from story_service.devtools import story_folder

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
STORIES = sorted(p for p in (ROOT / "stories").iterdir() if p.is_dir())
needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")


@needs_ffmpeg
@pytest.mark.parametrize("folder", STORIES, ids=lambda p: p.name)
def test_story_builds(folder, tmp_path):
    story = story_folder.load(folder)
    res = story_folder.build(story)
    index = story_folder.write(res, tmp_path / folder.name)
    assert index.exists() and res.bundle.report.passed
    assert res.bundle.meta.scene_count == len(story.scene_ids)
    assert not [i for r in res.reports.values() for i in r.issues if i.severity in ("blocker", "major")]


@needs_ffmpeg
def test_one_scene_preview_builds():
    story = story_folder.only_scenes(story_folder.load(ROOT / "stories" / "plants"), ["s04"])
    res = story_folder.build(story, preview=True)
    assert res.bundle.meta.scene_count == 1 and res.bundle.meta.interaction_count == 1
