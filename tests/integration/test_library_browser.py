"""Library acceptance in Chromium (Section 6.8): contact sheets, their review, and the joints check."""

import pytest

from story_service.devtools.library import joints_problems, render_sheets, review_sheets
from story_service.providers.vlm import FakeVLM

pytestmark = pytest.mark.integration
pytest.importorskip("playwright.sync_api")


def test_joints_hold_in_every_pose():
    assert joints_problems() == []


def test_contact_sheets_and_review(tmp_path):
    paths = render_sheets(tmp_path)
    assert {"backgrounds", "props"} <= set(paths) and sum(n.startswith("character_") for n in paths) == 12
    assert all(p.stat().st_size > 10_000 for p in paths.values())
    scores = review_sheets(paths, FakeVLM(score=5))
    assert all(score >= 4 for score, _ in scores.values())
    low = review_sheets({"props": paths["props"]}, FakeVLM(score=3))
    assert low["props"][0] < 4
