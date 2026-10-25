"""Story folders: a story is built from its JSON files alone (devtools/story_folder.py, scripts/build_story.py)."""

import json
import shutil
from pathlib import Path

import pytest

import golden
from story_service.devtools import story_folder
from story_service.devtools.story_folder import StoryFolderError

ROOT = Path(__file__).resolve().parents[2]
STORIES = sorted(p for p in (ROOT / "stories").iterdir() if p.is_dir())


def copy_golden(tmp_path) -> Path:
    folder = tmp_path / "story"
    shutil.copytree(golden.GOLDEN, folder)
    return folder


def test_the_example_stories_are_there():
    assert {p.name for p in STORIES} >= {"plants", "percent", "clock"}


@pytest.mark.parametrize("folder", STORIES, ids=lambda p: p.name)
def test_every_story_folder_loads_with_a_plan_per_scene(folder):
    story = story_folder.load(folder)
    assert story.scene_ids and set(story.plans) == set(story.scene_ids)
    speakers = {ln.speaker for s in story.script.scenes for ln in s.lines} - {"narrator"}
    assert speakers <= {c.key for c in story.cast.characters}


def test_missing_file_is_named(tmp_path):
    folder = copy_golden(tmp_path)
    (folder / "cast.json").unlink()
    with pytest.raises(StoryFolderError, match="cast.json is missing"):
        story_folder.load(folder)


def test_broken_json_says_where(tmp_path):
    folder = copy_golden(tmp_path)
    (folder / "script.json").write_text('{"title": "x",', encoding="utf-8")
    with pytest.raises(StoryFolderError, match=r"script.json is not valid JSON \(line 1"):
        story_folder.load(folder)


def test_schema_error_names_the_field(tmp_path):
    folder = copy_golden(tmp_path)
    data = json.loads((folder / "intake.json").read_text(encoding="utf-8"))
    data["language"] = "fr"
    (folder / "intake.json").write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(StoryFolderError, match="intake.json: language"):
        story_folder.load(folder)


def test_one_scene_preview_and_unknown_scene():
    story = story_folder.load(golden.GOLDEN)
    one = story_folder.only_scenes(story, ["s03"])
    assert one.scene_ids == ["s03"] and set(one.plans) == {"s03"}
    with pytest.raises(StoryFolderError, match="no scene s09; this story has s01, s02, s03, s04, s05"):
        story_folder.only_scenes(story, ["s09"])


def test_a_missing_plan_needs_the_director(tmp_path):
    folder = copy_golden(tmp_path)
    (folder / "plans" / "scene_plan_s02.json").unlink()
    story = story_folder.load(folder)
    with pytest.raises(StoryFolderError, match="no director plan for s02"):
        story_folder.build(story)
