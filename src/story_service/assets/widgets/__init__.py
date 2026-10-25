"""Parametric widgets (Section 6.6): small drawings made by code from parameters (a pie cut in 4 with 1 filled, a number
line, a clock at 3:30…). Each widget function returns an SVG group in its own local box (default 400x300) plus that box;
the composer places the box's CENTER at the planned (x, y).

Rules: labels use Cairo at 44 px or more, numbers follow DISPLAY_DIGITS, colors are tokens only. A widget never changes
its parameters over time: showing 1/4 becoming 2/4 is two widget instances that cues show and hide."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

from ...validators.text import to_display_digits

FONT = "Cairo"
MIN_TEXT = 44
SHAPES = ("triangle", "square", "circle", "rectangle", "pentagon", "hexagon")


@dataclass
class Widget:
    id: str
    label: str
    params: dict  # JSON-schema-like description given to the LLM through the catalog
    render: Callable[..., tuple[str, tuple[float, float, float, float]]]
    check: Callable[[dict], list[str]]
    bbox: tuple[float, float, float, float] = (0, 0, 400, 300)
    panel: bool = True  # drawn on a paper card so it reads as a teaching aid, not part of the room


def _n(v, digits: str) -> str:
    s = f"{v:g}" if isinstance(v, float) else str(v)
    return to_display_digits(s, digits)


def _is_rtl(s: str) -> bool:
    return any("؀" <= c <= "ۿ" or "ݐ" <= c <= "ݿ" for c in s)


def _text(x, y, s, size=MIN_TEXT, anchor="middle", fill="var(--ink)", weight=700) -> str:
    """One label. The direction is set on the label itself (Arabic words right-to-left, numbers left-to-right), so
    `text-anchor` means the same thing whatever the page direction is."""
    rtl = _is_rtl(s)
    if anchor != "middle" and rtl:  # in rtl, "start" is the right edge: keep the visual side
        anchor = "end" if anchor == "start" else "start"
    s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return (
        f'<text x="{x:.1f}" y="{y:.1f}" font-family="{FONT}" font-size="{size}" font-weight="{weight}" '
        f'text-anchor="{anchor}" direction="{"rtl" if rtl else "ltr"}" fill="{fill}">{s}</text>'
    )


def text_width(s: str, size: float = MIN_TEXT) -> float:
    """A safe estimate of the rendered width (Cairo bold: about 0.58 em per character, 0.3 em per space)."""
    return sum(0.3 if c == " " else 0.58 for c in s) * size


def _int(p, k, lo, hi, errors, required=True):
    v = p.get(k)
    if v is None:
        if required:
            errors.append(f"{k} is required")
        return None
    if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
        errors.append(f"{k} must be an integer {lo}..{hi}")
        return None
    return v


def _num(p, k, errors, required=True):
    v = p.get(k)
    if v is None:
        if required:
            errors.append(f"{k} is required")
        return None
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        errors.append(f"{k} must be a number")
        return None
    return v


# ----------------------------------------------------------------------------------------------------- fractions
def _check_fraction(p):
    e = []
    parts = _int(p, "parts", 2, 12, e)
    filled = _int(p, "filled", 0, 12, e)
    if parts is not None and filled is not None and filled > parts:
        e.append("filled must be between 0 and parts")
    return e


def pie_fraction(p, digits="western"):
    parts, filled = p["parts"], p["filled"]
    cx, cy, r = 200, 150, 128
    out = [f'<circle cx="{cx}" cy="{cy}" r="{r + 8}" fill="var(--c-neutral-1-d)"/>']
    for i in range(parts):
        a0, a1 = -math.pi / 2 + 2 * math.pi * i / parts, -math.pi / 2 + 2 * math.pi * (i + 1) / parts
        x0, y0 = cx + r * math.cos(a0), cy + r * math.sin(a0)
        x1, y1 = cx + r * math.cos(a1), cy + r * math.sin(a1)
        large = 1 if a1 - a0 > math.pi else 0
        fill = "var(--c-primary)" if i < filled else "var(--paper)"
        out.append(
            f'<path class="slice{" on" if i < filled else ""}" d="M{cx} {cy} L{x0:.1f} {y0:.1f} '
            f'A{r} {r} 0 {large} 1 {x1:.1f} {y1:.1f} Z" fill="{fill}"/>'
        )
    for i in range(parts):  # separators, so every part reads as equal
        a = -math.pi / 2 + 2 * math.pi * i / parts
        out.append(
            f'<path d="M{cx} {cy} L{cx + r * math.cos(a):.1f} {cy + r * math.sin(a):.1f}" '
            f'stroke="var(--c-neutral-1-d)" stroke-width="6" stroke-linecap="round"/>'
        )
    return "".join(out), (64, 14, 336, 286)


def bar_fraction(p, digits="western"):
    parts, filled = p["parts"], p["filled"]
    x0, w, y, h, gap = 20, 360, 110, 80, 6
    cell = (w - gap * (parts - 1)) / parts
    out = [f'<rect x="{x0 - 8}" y="{y - 8}" width="{w + 16}" height="{h + 16}" rx="14" fill="var(--c-neutral-1-d)"/>']
    for i in range(parts):
        fill = "var(--c-secondary)" if i < filled else "var(--paper)"
        out.append(
            f'<rect class="cell{" on" if i < filled else ""}" x="{x0 + i * (cell + gap):.1f}" y="{y}" '
            f'width="{cell:.1f}" height="{h}" rx="6" fill="{fill}"/>'
        )
    return "".join(out), (12, 102, 388, 198)


# ----------------------------------------------------------------------------------------------------- number line
def _check_number_line(p):
    e = []
    lo, hi, step = _num(p, "min", e), _num(p, "max", e), _num(p, "step", e)
    if None not in (lo, hi, step):
        if step <= 0 or hi <= lo:
            e.append("needs min < max and step > 0")
        elif (hi - lo) / step > 20 + 1e-9:
            e.append("at most 20 steps")
        elif (hi - lo) / step < 1 - 1e-9:
            e.append("step is larger than max - min")
        marks = p.get("marks", []) or []
        if not isinstance(marks, list):
            return [*e, "marks must be a list of numbers"]
        for m in marks:
            if not isinstance(m, (int, float)) or not lo <= m <= hi:
                e.append(f"mark {m!r} outside min..max")
        h = p.get("highlight")
        if h is not None and (not isinstance(h, (int, float)) or not lo <= h <= hi):
            e.append("highlight outside min..max")
    return e


def number_line(p, digits="western"):
    lo, hi, step = p["min"], p["max"], p["step"]
    marks, hl = p.get("marks") or [], p.get("highlight")
    n = round((hi - lo) / step)
    x0, x1, y = 30, 370, 150
    px = lambda v: x0 + (v - lo) / (hi - lo) * (x1 - x0)  # noqa: E731
    out = [f'<rect x="{x0 - 10}" y="{y - 5}" width="{x1 - x0 + 20}" height="10" rx="5" fill="var(--ink)"/>']
    widest = max(text_width(_n(lo + i * step, digits)) for i in range(n + 1))
    every = max(1, math.ceil((widest + 12) / ((x1 - x0) / n)))  # never let labels touch
    for i in range(n + 1):
        v = lo + i * step
        out.append(f'<rect x="{px(v) - 3:.1f}" y="{y - 22}" width="6" height="44" rx="3" fill="var(--ink)"/>')
        if i % every == 0 or i == n:
            out.append(_text(px(v), y + 74, _n(round(v, 6), digits)))
    for m in marks:
        out.append(f'<circle cx="{px(m):.1f}" cy="{y}" r="14" fill="var(--c-primary)"/>')
    if hl is not None:
        out.append(
            f'<circle class="hl" cx="{px(hl):.1f}" cy="{y - 54}" r="20" fill="var(--c-accent)"/>'
            f'<path d="M{px(hl) - 12:.1f} {y - 40} L{px(hl) + 12:.1f} {y - 40} L{px(hl):.1f} {y - 18} Z" fill="var(--c-accent)"/>'
        )
    return "".join(out), (14, 70, 386, 236)


# ----------------------------------------------------------------------------------------------------- bar chart
def _check_bar_chart(p):
    e = []
    labels, values = p.get("labels"), p.get("values")
    if not isinstance(labels, list) or not isinstance(values, list) or not labels:
        return ["labels and values are required lists"]
    if len(labels) > 6 or len(values) != len(labels):
        e.append("1 to 6 labels, one value each")
    if any(not isinstance(v, (int, float)) or v < 0 for v in values):
        e.append("values must be numbers >= 0")
    if any(len(str(lab).split()) > 4 for lab in labels):
        e.append("a label has more than 4 words")
    return e


def bar_chart(p, digits="western"):
    labels, values = [to_display_digits(str(x), digits) for x in p["labels"]], p["values"]
    col = max(110, max(text_width(lab) for lab in labels) + 20)
    w = col * len(labels)
    top, base = 70, 300
    vmax = max(values) or 1
    colors = ["c-primary", "c-secondary", "c-accent", "c-support", "c-primary-d", "c-secondary-d"]
    out = [f'<rect x="0" y="{base}" width="{w:.1f}" height="8" rx="4" fill="var(--ink)"/>']
    for i, (lab, v) in enumerate(zip(labels, values)):
        cx = col * i + col / 2
        h = (base - top - 50) * v / vmax
        out.append(
            f'<rect class="bar" x="{cx - 36:.1f}" y="{base - h:.1f}" width="72" height="{h:.1f}" rx="8" '
            f'fill="var(--{colors[i % len(colors)]})"/>'
        )
        out.append(_text(cx, base - h - 14, _n(v, digits)))
        out.append(_text(cx, base + 58, lab))
    return "".join(out), (0, top - 40, w, base + 72)


# ----------------------------------------------------------------------------------------------------- balance scale
def _check_balance(p):
    e = []
    _int(p, "left_count", 0, 12, e)
    _int(p, "right_count", 0, 12, e)
    return e


def balance_scale(p, digits="western"):
    left, right = p["left_count"], p["right_count"]
    tilt = max(-12, min(12, (right - left) * 4))  # degrees; the heavier side goes down
    cx, cy = 200, 110
    out = [
        '<path d="M150 290 L250 290 L230 262 L170 262 Z" fill="var(--c-neutral-2-d)"/>',
        f'<rect x="194" y="{cy}" width="12" height="156" fill="var(--c-neutral-2)"/>',
        f'<g transform="rotate({tilt} {cx} {cy})">',
        f'<rect x="40" y="{cy - 7}" width="320" height="14" rx="7" fill="var(--c-neutral-2-d)"/>',
    ]
    for side, count in ((60, left), (340, right)):
        out.append(
            f'<g transform="rotate({-tilt} {side} {cy})">'
            f'<path d="M{side} {cy} L{side - 44} {cy + 70} M{side} {cy} L{side + 44} {cy + 70}" '
            f'stroke="var(--c-neutral-2)" stroke-width="4"/>'
            f'<path d="M{side - 56} {cy + 70} L{side + 56} {cy + 70} Q{side + 48} {cy + 96} {side} {cy + 96} '
            f'Q{side - 48} {cy + 96} {side - 56} {cy + 70} Z" fill="var(--c-accent)"/>'
        )
        for k in range(count):
            row, colk = divmod(k, 4)
            out.append(
                f'<circle cx="{side - 33 + colk * 22}" cy="{cy + 58 - row * 20}" r="10" fill="var(--c-primary)"/>'
            )
        out.append("</g>")
    out.append(f'</g><circle cx="{cx}" cy="{cy}" r="12" fill="var(--c-accent-d)"/>')
    return "".join(out), (0, 20, 400, 292)


# ----------------------------------------------------------------------------------------------------- grid array
def _check_grid(p):
    e = []
    rows = _int(p, "rows", 1, 10, e)
    _int(p, "cols", 1, 10, e)
    hr = p.get("highlight_rows")

    def bad_row(r):
        return not isinstance(r, int) or (rows is not None and not 1 <= r <= rows)

    if hr is not None and (not isinstance(hr, list) or any(bad_row(r) for r in hr)):
        e.append("highlight_rows must list row numbers from 1")
    return e


def grid_array(p, digits="western"):
    rows, cols, hr = p["rows"], p["cols"], set(p.get("highlight_rows") or [])
    cell = min(320 / cols, 240 / rows)
    w, h = cell * cols, cell * rows
    x0, y0 = 200 - w / 2, 150 - h / 2
    out = [
        f'<rect x="{x0 - 12:.1f}" y="{y0 - 12:.1f}" width="{w + 24:.1f}" height="{h + 24:.1f}" rx="14" fill="var(--c-neutral-1)"/>'
    ]
    for r in range(rows):
        for c in range(cols):
            fill = "var(--c-primary)" if (r + 1) in hr else "var(--c-support)"
            out.append(
                f'<circle cx="{x0 + cell * (c + 0.5):.1f}" cy="{y0 + cell * (r + 0.5):.1f}" r="{cell * 0.36:.1f}" fill="{fill}"/>'
            )
    return "".join(out), (x0 - 12, y0 - 12, x0 + w + 12, y0 + h + 12)


# ----------------------------------------------------------------------------------------------------- clock
def _check_clock(p):
    e = []
    _int(p, "hour", 0, 23, e)
    _int(p, "minute", 0, 59, e)
    return e


def clock_face(p, digits="western"):
    hour, minute = p["hour"] % 12, p["minute"]
    cx, cy, r = 200, 150, 140
    out = [
        f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="var(--c-primary)"/>',
        f'<circle cx="{cx}" cy="{cy}" r="{r - 14}" fill="var(--paper)"/>',
    ]
    for i in range(12):
        a = math.radians(i * 30 - 90)
        if i % 3:
            out.append(
                f'<circle cx="{cx + 108 * math.cos(a):.1f}" cy="{cy + 108 * math.sin(a):.1f}" r="5" fill="var(--ink)"/>'
            )
        else:
            out.append(_text(cx + 98 * math.cos(a), cy + 98 * math.sin(a) + 16, _n(12 if i == 0 else i, digits)))
    ha, ma = math.radians((hour + minute / 60) * 30 - 90), math.radians(minute * 6 - 90)
    out.append(
        f'<path d="M{cx} {cy} L{cx + 58 * math.cos(ha):.1f} {cy + 58 * math.sin(ha):.1f}" stroke="var(--ink)" stroke-width="12" stroke-linecap="round"/>'
    )
    out.append(
        f'<path d="M{cx} {cy} L{cx + 92 * math.cos(ma):.1f} {cy + 92 * math.sin(ma):.1f}" stroke="var(--c-support)" stroke-width="8" stroke-linecap="round"/>'
    )
    out.append(f'<circle cx="{cx}" cy="{cy}" r="10" fill="var(--c-primary-d)"/>')
    return "".join(out), (60, 10, 340, 290)


# ----------------------------------------------------------------------------------------------------- ruler
def _check_ruler(p):
    e = []
    n = _int(p, "length_cm", 1, 30, e)
    m = p.get("mark_cm")
    if m is not None and (not isinstance(m, (int, float)) or n is not None and not 0 <= m <= n):
        e.append("mark_cm must be between 0 and length_cm")
    return e


def ruler(p, digits="western"):
    n, mark = p["length_cm"], p.get("mark_cm")
    x0, x1, y = 20, 380, 120
    u = (x1 - x0) / n
    out = [
        f'<rect x="{x0 - 10}" y="{y}" width="{x1 - x0 + 20}" height="90" rx="10" fill="var(--c-accent)"/>',
        f'<rect x="{x0 - 10}" y="{y + 64}" width="{x1 - x0 + 20}" height="26" rx="8" fill="var(--c-accent-d)"/>',
    ]
    step = 1 if n <= 10 else 5
    for i in range(n + 1):
        out.append(
            f'<rect x="{x0 + i * u - 2:.1f}" y="{y}" width="4" height="{34 if i % step == 0 else 20}" fill="var(--ink)"/>'
        )
    labels = [0, n] if n > 10 else list(range(0, n + 1, 2 if n > 6 else 1))
    for i in labels:
        out.append(_text(x0 + i * u, y + 84, _n(i, digits), anchor="middle"))
    if mark is not None:
        mx = x0 + mark * u
        out.append(
            f'<path d="M{mx - 16:.1f} {y - 40} L{mx + 16:.1f} {y - 40} L{mx:.1f} {y - 6} Z" fill="var(--c-primary)"/>'
        )
    return "".join(out), (10, y - 44, 390, y + 92)


# ----------------------------------------------------------------------------------------------------- coins
def _check_coins(p):
    e = []
    _int(p, "count", 1, 20, e)
    return e


def coin_stack(p, digits="western"):
    count = p["count"]
    stacks = math.ceil(count / 10)
    out = []
    for s in range(stacks):
        in_stack = min(10, count - s * 10)
        cx = 200 + (s - (stacks - 1) / 2) * 120
        for k in range(in_stack):
            y = 260 - k * 18
            out.append(
                f'<ellipse cx="{cx:.1f}" cy="{y + 6}" rx="48" ry="14" fill="var(--c-accent-d)"/>'
                f'<ellipse cx="{cx:.1f}" cy="{y}" rx="48" ry="14" fill="var(--c-accent)"/>'
            )
    top = 260 - 9 * 18 - 14 if count >= 10 else 260 - (count - 1) * 18 - 14
    return "".join(out), (200 - stacks * 60, top, 200 + stacks * 60, 280)


# ----------------------------------------------------------------------------------------------------- thermometer
def _check_thermo(p):
    e = []
    v, lo, hi = _num(p, "value", e), _num(p, "min", e), _num(p, "max", e)
    if None not in (v, lo, hi) and not lo <= v <= hi:
        e.append("value must be between min and max")
    if None not in (lo, hi) and hi <= lo:
        e.append("min must be below max")
    return e


def thermometer(p, digits="western"):
    v, lo, hi = p["value"], p["min"], p["max"]
    top, bottom = 30, 230
    level = bottom - (v - lo) / (hi - lo) * (bottom - top)
    out = [
        f'<rect x="170" y="{top - 10}" width="60" height="{bottom - top + 30}" rx="30" fill="var(--paper-d)"/>',
        f'<rect x="186" y="{level:.1f}" width="28" height="{bottom - level + 10:.1f}" rx="14" fill="var(--c-primary)"/>',
        f'<circle cx="200" cy="{bottom + 30}" r="40" fill="var(--c-primary)"/>',
        _text(250, top + 16, _n(hi, digits), anchor="start"),
        _text(250, bottom + 16, _n(lo, digits), anchor="start"),
    ]
    return "".join(out), (140, top - 20, 330, bottom + 72)


# ----------------------------------------------------------------------------------------------------- shapes
def _check_shapes(p):
    shapes = p.get("shapes")
    if not isinstance(shapes, list) or not 1 <= len(shapes) <= 6:
        return ["shapes must list 1 to 6 shapes"]
    return [f"unknown shape {s!r}" for s in shapes if s not in SHAPES]


def _poly(cx, cy, r, n, rot=-90):
    pts = " ".join(
        f"{cx + r * math.cos(math.radians(rot + 360 * i / n)):.1f},{cy + r * math.sin(math.radians(rot + 360 * i / n)):.1f}"
        for i in range(n)
    )
    return pts


def shape_set(p, digits="western"):
    shapes = p["shapes"]
    colors = ["c-primary", "c-secondary", "c-accent", "c-support", "c-primary-d", "c-secondary-d"]
    cols = min(3, len(shapes))
    rows = math.ceil(len(shapes) / cols)
    cell = 130
    w, h = cols * cell, rows * cell
    out = []
    for i, s in enumerate(shapes):
        r_, c_ = divmod(i, cols)
        cx, cy = c_ * cell + cell / 2, r_ * cell + cell / 2
        f = f'fill="var(--{colors[i % len(colors)]})"'
        out.append(
            {
                "triangle": f'<polygon points="{_poly(cx, cy + 8, 56, 3)}" {f}/>',
                "square": f'<rect x="{cx - 46}" y="{cy - 46}" width="92" height="92" rx="6" {f}/>',
                "circle": f'<circle cx="{cx}" cy="{cy}" r="50" {f}/>',
                "rectangle": f'<rect x="{cx - 56}" y="{cy - 34}" width="112" height="68" rx="6" {f}/>',
                "pentagon": f'<polygon points="{_poly(cx, cy + 4, 54, 5)}" {f}/>',
                "hexagon": f'<polygon points="{_poly(cx, cy, 54, 6, 0)}" {f}/>',
            }[s]
        )
    return "".join(out), (0, 0, w, h)


# ----------------------------------------------------------------------------------------------------- timeline
def _check_timeline(p):
    ev = p.get("events")
    if not isinstance(ev, list) or not 1 <= len(ev) <= 5:
        return ["events must list 1 to 5 labels"]
    return [f"event {e!r} has more than 4 words" for e in ev if len(str(e).split()) > 4]


def timeline_axis(p, digits="western"):
    ev = [to_display_digits(str(e), digits) for e in p["events"]]
    col = max(170, max(text_width(e) for e in ev) + 24)
    w = col * len(ev)
    y = 150
    out = [f'<rect x="0" y="{y - 6}" width="{w:.1f}" height="12" rx="6" fill="var(--ink)"/>']
    for i, e in enumerate(ev):
        cx = col * i + col / 2
        out.append(f'<circle cx="{cx:.1f}" cy="{y}" r="18" fill="var(--c-primary)"/>')
        out.append(_text(cx, y - 40 if i % 2 else y + 76, e))
    return "".join(out), (0, y - 90, w, y + 92)


# ----------------------------------------------------------------------------------------------------- label card
def _check_label(p):
    t = p.get("text")
    if not isinstance(t, str) or not t.strip():
        return ["text is required"]
    return ["at most 4 words"] if len(t.split()) > 4 else []


def label_card(p, digits="western"):
    t = to_display_digits(str(p["text"]), digits)
    w = max(160, text_width(t, 48) + 64)
    out = [
        f'<rect x="{200 - w / 2:.1f}" y="104" width="{w:.1f}" height="92" rx="22" fill="var(--paper)"/>',
        f'<rect x="{200 - w / 2:.1f}" y="184" width="{w:.1f}" height="12" rx="6" fill="var(--c-neutral-1)"/>',
        _text(200, 166, t, size=48),
    ]
    return "".join(out), (200 - w / 2, 104, 200 + w / 2, 196)


_I = {"type": "integer"}
_N = {"type": "number"}
WIDGETS: dict[str, Widget] = {
    w.id: w
    for w in [
        Widget(
            "widget_pie_fraction",
            "A circle cut in equal parts, some filled",
            {"parts": {**_I, "minimum": 2, "maximum": 12}, "filled": {**_I, "minimum": 0, "maximum": 12}},
            pie_fraction,
            _check_fraction,
        ),
        Widget(
            "widget_bar_fraction",
            "A bar cut in equal parts, some filled",
            {"parts": {**_I, "minimum": 2, "maximum": 12}, "filled": {**_I, "minimum": 0, "maximum": 12}},
            bar_fraction,
            _check_fraction,
        ),
        Widget(
            "widget_number_line",
            "Number line with marks",
            {"min": _N, "max": _N, "step": _N, "marks": {"type": "array", "items": _N}, "highlight": _N},
            number_line,
            _check_number_line,
        ),
        Widget(
            "widget_bar_chart",
            "Bar chart (up to 6 bars)",
            {
                "labels": {"type": "array", "maxItems": 6, "items": {"type": "string"}},
                "values": {"type": "array", "maxItems": 6, "items": _N},
            },
            bar_chart,
            _check_bar_chart,
        ),
        Widget(
            "widget_balance_scale",
            "Balance scale with weights on each side",
            {"left_count": {**_I, "minimum": 0, "maximum": 12}, "right_count": {**_I, "minimum": 0, "maximum": 12}},
            balance_scale,
            _check_balance,
        ),
        Widget(
            "widget_grid_array",
            "Grid of dots in rows and columns",
            {
                "rows": {**_I, "minimum": 1, "maximum": 10},
                "cols": {**_I, "minimum": 1, "maximum": 10},
                "highlight_rows": {"type": "array", "items": _I},
            },
            grid_array,
            _check_grid,
        ),
        Widget(
            "widget_clock_face",
            "Clock showing a time",
            {"hour": {**_I, "minimum": 0, "maximum": 23}, "minute": {**_I, "minimum": 0, "maximum": 59}},
            clock_face,
            _check_clock,
        ),
        Widget(
            "widget_ruler",
            "Ruler in centimeters",
            {"length_cm": {**_I, "minimum": 1, "maximum": 30}, "mark_cm": _N},
            ruler,
            _check_ruler,
        ),
        Widget(
            "widget_coin_stack",
            "Stack of coins",
            {"count": {**_I, "minimum": 1, "maximum": 20}},
            coin_stack,
            _check_coins,
        ),
        Widget("widget_thermometer", "Thermometer", {"value": _N, "min": _N, "max": _N}, thermometer, _check_thermo),
        Widget(
            "widget_shape_set",
            "Set of geometric shapes",
            {"shapes": {"type": "array", "maxItems": 6, "items": {"enum": list(SHAPES)}}},
            shape_set,
            _check_shapes,
        ),
        Widget(
            "widget_timeline_axis",
            "Timeline with up to 5 events",
            {"events": {"type": "array", "maxItems": 5, "items": {"type": "string"}}},
            timeline_axis,
            _check_timeline,
        ),
        Widget(
            "widget_label_card",
            "Card with a short label (up to 4 words)",
            {"text": {"type": "string"}},
            label_card,
            _check_label,
            panel=False,
        ),
    ]
}


def render_widget(ref: str, params: dict, digits: str = "western") -> tuple[str, tuple[float, float, float, float]]:
    """Return `(markup, local_bbox)` or raise ValueError with the parameter problems."""
    w = WIDGETS.get(ref)
    if w is None:
        raise ValueError(f"unknown widget {ref!r}")
    try:  # params come from an LLM: any surprise in them is a parameter problem, never a crash
        errors = w.check(params or {})
        if not errors:
            markup, bbox = w.render(params, digits)
    except Exception as e:  # noqa: BLE001
        errors = [f"invalid parameters ({type(e).__name__}: {e})"]
    if errors:
        raise ValueError(f"{ref}: " + "; ".join(errors))
    return _on_panel(markup, bbox) if w.panel else (markup, bbox)


PANEL_PAD = 20


def _on_panel(markup: str, bbox) -> tuple[str, tuple[float, float, float, float]]:
    """Put a widget on a paper card in the label card's style (a flat card with a darker lip at the bottom), so it
    stands out from any background. The returned box includes the card."""
    x0, y0, x1, y1 = bbox[0] - PANEL_PAD, bbox[1] - PANEL_PAD, bbox[2] + PANEL_PAD, bbox[3] + PANEL_PAD + 12
    w, h = x1 - x0, y1 - y0
    card = (
        f'<rect class="panel" x="{x0:.1f}" y="{y0:.1f}" width="{w:.1f}" height="{h:.1f}" rx="24" '
        f'fill="var(--c-neutral-1)"/>'
        f'<rect class="panel" x="{x0:.1f}" y="{y0:.1f}" width="{w:.1f}" height="{h - 12:.1f}" rx="24" '
        f'fill="var(--paper)"/>'
    )
    return card + markup, (x0, y0, x1, y1)


def widget_texts(markup: str) -> list[tuple[str, float]]:
    """The (text, font-size) pairs inside a rendered widget, for the text-size checks of S7 and S11."""
    import re

    return [(m.group(2), float(m.group(1))) for m in re.finditer(r'font-size="([\d.]+)"[^>]*>([^<]*)</text>', markup)]
