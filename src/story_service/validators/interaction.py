"""S11 interaction tests (Section 8.5), driven only through the `window.__story` hooks and the visible controls.

For every interaction: it opens on its own when the story plays into it, its card fits the screen, a wrong answer
shows feedback, the right answer shows Continue, Continue makes the clock run again, Skip appears after 8 s (with
Playwright's fake clock). Then a full playthrough steps a fake clock through the whole story, answering every
interaction, and expects the end screen. An overlay that cannot be left is a dead end (blocker)."""

from __future__ import annotations

from .render import Reports, StoryPage

# audio is replaced by a refusal in the fake-clock runs: the clock then leads (audio cannot follow fake time)
NO_AUDIO = "HTMLMediaElement.prototype.play = function () { return Promise.reject(new Error('muted for the test')); };"


def _correct_actions(it: dict) -> list[dict]:
    t = it["type"]
    if t == "choose_answer":
        return [{"option_id": next(o["id"] for o in it["options"] if o["correct"])}]
    if t == "predict":
        return [{"option_id": it["expected_option_id"]}]
    if t == "drag_match":
        return [{"drop": {"item_id": p["id"], "target_id": p["id"]}} for p in it["pairs"]]
    if t == "tap_reveal":
        return [{"reveal": b["element_id"]} for b in sorted(it.get("bindings", []), key=lambda b: b["index"])]
    return []


def _wrong_action(it: dict) -> dict | None:
    if it["type"] == "choose_answer":
        return {"option_id": next(o["id"] for o in it["options"] if not o["correct"])}
    if it["type"] == "drag_match" and len(it["pairs"]) > 1:
        a, b = it["pairs"][0], it["pairs"][1]
        return {"drop": {"item_id": a["id"], "target_id": b["id"]}}
    return None


def _card_fits(sp: StoryPage) -> tuple[bool, str]:
    r = sp.js(
        """() => { const c = document.querySelector('#overlay .card'); if (!c) return null;
        const r = c.getBoundingClientRect(); return {l: r.left, t: r.top, r: r.right, b: r.bottom, W: innerWidth, H: innerHeight}; }"""
    )
    if r is None:
        return False, "no card"
    ok = r["l"] >= -1 and r["t"] >= -1 and r["r"] <= r["W"] + 1 and r["b"] <= r["H"] + 1
    return ok, f"card {round(r['l'])},{round(r['t'])}-{round(r['r'])},{round(r['b'])} in {r['W']}x{r['H']}"


def _small_controls(sp: StoryPage) -> list[str]:
    return sp.js(
        """() => [...document.querySelectorAll('#overlay button, .hotspot')].filter(b => !b.hidden && b.getBoundingClientRect().width)
           .map(b => [b.className.baseVal !== undefined ? b.className.baseVal : b.className, b.getBoundingClientRect()])
           .filter(([, r]) => r.width < 43.5 || r.height < 43.5).map(([n, r]) => `${n} ${Math.round(r.width)}x${Math.round(r.height)}`)"""
    )


def test_interactions(rep: Reports, browser, url: str, viewport: tuple[int, int], vp: str, *, full: bool) -> None:
    """Steps 1-3 and 5 of Section 8.5 on one viewport (fit and touch size on every viewport; the answer paths only
    when `full`, they do not depend on the screen size)."""
    sp = StoryPage(browser, url, viewport)
    try:
        for it in [s["interaction"] for s in sp.data["scenes"] if s.get("interaction") and rep.covers(s["id"])]:
            r = rep.of(_scene_of(sp.data, it["id"]))
            iid = it["id"]
            sp.js("window.__story.pause()")
            sp.seek(it["at_ms"] - 2000)
            sp.js("window.__story.play()")
            opened = sp.wait_until("window.__story.getState().overlayOpen", 3000)
            r.add(
                f"interaction_opens_{iid}_{vp}",
                opened,
                "major",
                f"{iid} did not open within 3 s of playing",
                code="INTERACTION_NOT_OPENED",
                suspect_stage="S10_assembler",
            )
            if not opened:
                sp.js(f"window.__story.openInteraction('{iid}')")
            ok, detail = _card_fits(sp)
            r.add(
                f"interaction_fits_{iid}_{vp}",
                ok,
                "major",
                f"{iid}: {detail}",
                code="INTERACTION_FIT",
                suspect_stage="S10_assembler",
            )
            small = _small_controls(sp)
            r.add(
                f"interaction_touch_{iid}_{vp}",
                not small,
                "major",
                f"{iid}: controls under 44x44 px: {small[:4]}",
                code="TOUCH_TARGET",
                suspect_stage="S10_assembler",
            )
            if not full:
                continue
            wrong = _wrong_action(it)
            if wrong is not None:
                sp.js(f"window.__story.answerInteraction('{iid}', {_js(wrong)})")
                fb = sp.js("(document.querySelector('#overlay .fb') || {}).textContent || ''")
                r.add(
                    f"interaction_wrong_{iid}",
                    bool(fb.strip()),
                    "major",
                    f"{iid}: no feedback after a wrong answer",
                    code="INTERACTION_FEEDBACK",
                    suspect_stage="S10_assembler",
                )
            for act in _correct_actions(it):
                sp.js(f"window.__story.answerInteraction('{iid}', {_js(act)})")
            go = sp.page.locator("#overlay .go")
            visible = go.count() == 1 and go.is_visible()
            r.add(
                f"interaction_continue_{iid}",
                visible,
                "blocker",
                f"{iid}: Continue never appears (dead end)",
                code="DEAD_END",
                suspect_stage="S10_assembler",
            )
            if visible:
                t0 = sp.js("window.__story.getTime()")
                go.click()
                sp.page.wait_for_timeout(2000)
                moved = sp.js("window.__story.getTime()") - t0
                r.add(
                    f"interaction_resumes_{iid}",
                    moved >= 500,
                    "major",
                    f"{iid}: the clock moved {moved:.0f} ms in 2 s after Continue",
                    code="NO_RESUME",
                    suspect_stage="S10_assembler",
                )
            sp.js("window.__story.pause()")
        errors = sp.errors + sp.state().get("errors", [])
        rep.bundle.add(
            f"interaction_errors_{vp}",
            not errors,
            "major",
            "; ".join(errors[:3]),
            code="PAGE_ERRORS",
            suspect_stage="S10_assembler",
        )
    finally:
        sp.close()


def test_skip_and_playthrough(rep: Reports, browser, url: str, viewport: tuple[int, int]) -> None:
    """Step 4 (Skip after 8 s) and the full playthrough, both on Playwright's fake clock."""
    sp = StoryPage(browser, url, viewport, init_scripts=(NO_AUDIO,), mobile=False)
    try:
        sp.page.clock.install()
        sp.page.reload()
        sp.wait_until("window.__story !== undefined", 10000)
        inters = [s["interaction"] for s in sp.data["scenes"] if s.get("interaction")]
        for it in [x for x in inters if rep.covers(_scene_of(sp.data, x["id"]))]:
            sp.js(f"window.__story.openInteraction('{it['id']}')")
            sp.page.clock.run_for(7500)
            early = sp.page.locator("#overlay .skip").is_visible()
            sp.page.clock.run_for(800)
            shown = sp.page.locator("#overlay .skip").is_visible()
            rep.of(_scene_of(sp.data, it["id"])).add(
                f"interaction_skip_{it['id']}",
                shown and not early,
                "major",
                f"{it['id']}: Skip should appear after 8 s (before: {early}, after: {shown})",
                code="NO_SKIP",
                suspect_stage="S10_assembler",
            )
        # full playthrough
        sp.js("window.__story.seek(0); window.__story.play()")
        dead, guard = [], 0
        while not sp.js("window.__story.getState().ended") and guard < 400:
            guard += 1
            sp.page.clock.run_for(1000)
            st = sp.state()
            if st["overlayOpen"]:
                it = next((x for x in inters if x["id"] == st.get("interaction")), None)
                for act in _correct_actions(it) if it else []:
                    sp.js(f"window.__story.answerInteraction('{it['id']}', {_js(act)})")
                go = sp.page.locator("#overlay .go")
                if go.count() == 1 and go.is_visible():
                    go.click()
                else:
                    dead.append(st.get("interaction") or f"t={st['t']}")
                    break
        ended = sp.js("window.__story.getState().ended") and sp.js("!document.getElementById('end').hidden")
        errors = sp.state().get("errors", []) + [e for e in sp.errors if "muted for the test" not in e]
        rep.bundle.add(
            "playthrough_dead_end",
            not dead,
            "blocker",
            f"the overlay could not be left: {dead}",
            code="DEAD_END",
            suspect_stage="S10_assembler",
        )
        rep.bundle.add(
            "playthrough_end",
            bool(ended) and not errors,
            "major",
            f"end screen shown: {bool(ended)}; errors: {errors[:3]}",
            code="PLAYTHROUGH",
            suspect_stage="S10_assembler",
        )
    finally:
        sp.close()


def _scene_of(data: dict, interaction_id: str) -> str | None:
    return next((s["id"] for s in data["scenes"] if (s.get("interaction") or {}).get("id") == interaction_id), None)


def _js(obj) -> str:
    import json

    return json.dumps(obj, ensure_ascii=False)
