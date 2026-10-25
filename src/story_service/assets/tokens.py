"""Design tokens (Section 6.2): every color in every asset is one of these CSS variables, never a raw hex or rgb().

Ages 6-8 and 9-11 use the bright column, 12-14 and 15-18 the calm one. Depth in the flat style comes from ONE darker
shade per shape, so every token also has a `-d` twin that is 18% darker (Section 6.1: "15 to 20% darker")."""

from __future__ import annotations

BRIGHT = {
    "ink": "#1E2A3A",
    "paper": "#FFF9F0",
    "c-primary": "#FF7A59",
    "c-secondary": "#2EC4B6",
    "c-accent": "#FFC93C",
    "c-support": "#6C63FF",
    "c-neutral-1": "#D9D4CC",
    "c-neutral-2": "#8E99A8",
    "sky-day": "#BEE9FF",
    "ground-day": "#A8E6A1",
    "sky-evening": "#FFD3A5",
    "ground-evening": "#8FD19E",
    "sky-night": "#1F2A55",
    "ground-night": "#2F4F4F",
}
CALM = {
    "ink": "#1E2A3A",
    "paper": "#F7F5F0",
    "c-primary": "#E4572E",
    "c-secondary": "#17BEBB",
    "c-accent": "#FFC914",
    "c-support": "#4B5D8C",
    "c-neutral-1": "#D9D4CC",
    "c-neutral-2": "#8E99A8",
    "sky-day": "#CFE8F7",
    "ground-day": "#B7D9B0",
    "sky-evening": "#F6C89F",
    "ground-evening": "#9CC49A",
    "sky-night": "#1B2340",
    "ground-night": "#2A3F3F",
}
SKIN = ["#FDDCC0", "#F5C6A0", "#E0A878", "#C68642", "#8D5524", "#5C3A21"]
HAIR = ["#1B1B1B", "#3B2A20", "#6B4423", "#A0522D", "#D2A24C", "#8A8A8A"]
CLOTHING_TOKENS = ["c-primary", "c-secondary", "c-accent", "c-support", "c-neutral-1", "c-neutral-2", "ink", "paper"]
DARK = 0.82
# how strongly the indoor `--light` tint shows at each time of day (window glow and lamp areas)
LIGHT_OPACITY = {"day": 0.0, "evening": 0.22, "night": 0.42}


def shade(hex_color: str, k: float = DARK) -> str:
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i : i + 2], 16) for i in (0, 2, 4))
    return "#{:02X}{:02X}{:02X}".format(*(max(0, min(255, round(c * k))) for c in (r, g, b)))


def tint(hex_color: str, paper: str, k: float = 0.3) -> str:
    """A light version of a color: k of the color over (1 - k) of paper (used for UI highlights, never in assets)."""
    a, b = hex_color.lstrip("#"), paper.lstrip("#")
    ca = [int(a[i : i + 2], 16) for i in (0, 2, 4)]
    cb = [int(b[i : i + 2], 16) for i in (0, 2, 4)]
    return "#{:02X}{:02X}{:02X}".format(*(round(x * k + y * (1 - k)) for x, y in zip(ca, cb)))


def column(age_group: str) -> dict[str, str]:
    return BRIGHT if age_group in ("6-8", "9-11") else CALM


def all_tokens(age_group: str = "9-11") -> dict[str, str]:
    """Every token name (without `--`) and its value, including the `-d` shades, skins and hairs."""
    out: dict[str, str] = {}
    for name, value in column(age_group).items():
        out[name], out[name + "-d"] = value, shade(value)
    for i, v in enumerate(SKIN):
        out[f"skin-{i}"], out[f"skin-{i}-d"] = v, shade(v)
    for i, v in enumerate(HAIR):
        out[f"hair-{i}"], out[f"hair-{i}-d"] = v, shade(v)
    return out


# names an asset may reference with var(): the table, the scene-level aliases and the per-actor variables
SCENE_VARS = {"sky", "sky-d", "ground", "ground-d", "light-o"}
ACTOR_VARS = {
    "skin",
    "skin-d",
    "hair",
    "hair-d",
    "top",
    "top-d",
    "bottom",
    "bottom-d",
    "shoe",
    "shoe-d",
    "leg",
    "blink",
}


def known_var_names(age_group: str = "9-11") -> set[str]:
    return set(all_tokens(age_group)) | SCENE_VARS | ACTOR_VARS


def tokens_css(age_group: str) -> str:
    """The `:root` block for the bundle plus one class per time of day that sets `--sky`, `--ground` and `--light-o`."""
    t = all_tokens(age_group)
    col = column(age_group)
    for name in ("c-primary", "c-secondary", "c-accent", "c-support"):
        t[name + "-l"] = tint(col[name], col["paper"])
    ink = col["ink"].lstrip("#")
    t["scrim"] = "rgba({},{},{},.55)".format(*(int(ink[i : i + 2], 16) for i in (0, 2, 4)))
    root = ";".join(f"--{k}:{v}" for k, v in t.items())
    tod = "".join(
        f".tod-{tod}{{--sky:var(--sky-{tod});--sky-d:var(--sky-{tod}-d);--ground:var(--ground-{tod});"
        f"--ground-d:var(--ground-{tod}-d);--light-o:{LIGHT_OPACITY[tod]}}}"
        for tod in ("day", "evening", "night")
    )
    return f":root{{{root}}}{tod}"


def actor_style(skin_tone: int, hair_color: int, top_token: str, bottom_token: str, shoe_token: str = "ink") -> str:
    """The per-character CSS variables: one design, the same in every scene (S5 rule)."""
    return (
        f"--skin:var(--skin-{skin_tone});--skin-d:var(--skin-{skin_tone}-d);"
        f"--hair:var(--hair-{hair_color});--hair-d:var(--hair-{hair_color}-d);"
        f"--top:var(--{top_token});--top-d:var(--{top_token}-d);"
        f"--bottom:var(--{bottom_token});--bottom-d:var(--{bottom_token}-d);"
        f"--shoe:var(--{shoe_token});--shoe-d:var(--{shoe_token}-d)"
    )
