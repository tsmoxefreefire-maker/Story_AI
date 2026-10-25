"""Render an SVG (a custom prop, a library asset) to PNG with headless Chromium, for the vision checks.

The SVG uses token colors (`var(--c-primary)`), so the page defines the palette of the story's age group first."""

from __future__ import annotations

import html

from ..assets.tokens import tokens_css


def svg_to_png(svg: str, age_group: str = "9-11", size: int = 400) -> bytes | None:
    """PNG bytes of `svg` drawn `size` px wide on a paper background, or None if Chromium is not available."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None
    page_html = (
        f"<!doctype html><meta charset='utf-8'><style>{tokens_css(age_group)}"
        f"html,body{{margin:0;background:var(--paper)}}svg{{display:block;width:{size}px;height:{size}px}}</style>"
        f"<body>{svg}</body>"
    )
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page(viewport={"width": size, "height": size})
                page.set_content(page_html)
                return page.screenshot(type="png")
            finally:
                browser.close()
    except Exception:  # noqa: BLE001  (no browser installed, sandbox limits…): the caller falls back
        return None


def escape_for_prompt(text: str) -> str:
    """Text placed inside <tags> of a prompt must not be able to close them."""
    return html.escape(text, quote=False)
