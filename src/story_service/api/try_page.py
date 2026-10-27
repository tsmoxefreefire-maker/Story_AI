"""The try page (اجتهاد, not in the plan): open the server's address and try module 6 without /docs.

`render(stories_dir)` gives the HTML of `try_page.html` with the story folders written into it (title, topic, age).
The page asks for the API key once (kept in that browser only, localStorage) and is automatic from then on: it plays
the newest published story, follows one that is being built, or builds the first folder when there is none. Any other
folder is one click. It uses the same /v1 routes as /docs (/v1/dev/stories, /v1/stories/{id} until done, access-link)
and plays the story inside the page. The page holds no secret: everything goes through the key the person types.

Plain Python (no FastAPI here), so it is tested without a server; the route is in routes_try.py."""

from __future__ import annotations

import json
from pathlib import Path

PAGE = Path(__file__).with_name("try_page.html")
MARK = "/*STORIES*/[]"

# The page only talks to this server (connect-src, frame-src 'self') and cannot itself be framed elsewhere.
HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; "
        "connect-src 'self'; frame-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


def story_cards(stories_dir: str | Path, folders: list[str]) -> list[dict]:
    """One card per story folder: its name and, when its files read, the story title, topic, age and scene count."""
    cards = []
    for name in folders:
        card = {"folder": name}
        try:
            intake = json.loads((Path(stories_dir) / name / "intake.json").read_text(encoding="utf-8"))
            script = json.loads((Path(stories_dir) / name / "script.json").read_text(encoding="utf-8"))
            card |= {
                "title": script.get("title"),
                "topic": intake.get("topic_title"),
                "age": intake.get("age_group"),
                "scenes": len(script.get("scenes", [])),
            }
        except (OSError, ValueError):
            pass  # a folder that does not read still gets a card with its name; building it reports the problem
        cards.append(card)
    return cards


def render(stories_dir: str | Path, folders: list[str]) -> str:
    """The page with the cards as a JSON literal. `<` is escaped so no text in a story can close the script."""
    data = json.dumps(story_cards(stories_dir, folders), ensure_ascii=False).replace("<", "\\u003c")
    html = PAGE.read_text(encoding="utf-8")
    if MARK not in html:
        raise RuntimeError("try_page.html lost its /*STORIES*/[] mark")
    return html.replace(MARK, data, 1)
