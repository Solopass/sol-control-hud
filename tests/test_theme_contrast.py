"""Every theme stays readable: body text and muted text >= 4.5:1 on a card, dim text and the accent (pill) colours
>= 3:1 (WCAG AA for normal / large-or-UI text). Added 10-19 with the dashboard theme pass (B3)."""
import pytest

from sol_control_hud.views.ticker_base import THEMES


def _lum(hex_colour: str) -> float:
    h = hex_colour.lstrip("#")
    rgb = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    rgb = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
    return 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]


def contrast(a: str, b: str) -> float:
    hi, lo = sorted((_lum(a), _lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


PAIRS = [("text_main", 4.5), ("text_muted", 4.5), ("text_dim", 3.0),
         ("accent_primary", 3.0), ("accent_green", 3.0), ("accent_amber", 3.0), ("accent_red", 3.0)]


@pytest.mark.parametrize("theme", sorted(THEMES))
@pytest.mark.parametrize("key,minimum", PAIRS)
def test_theme_colour_readable_on_card_and_page(theme, key, minimum):
    t = THEMES[theme]
    for bg in ("card", "bg"):
        ratio = contrast(t[key], t[bg])
        assert ratio >= minimum, f"{theme}: {key} {t[key]} on {bg} {t[bg]} is {ratio:.2f}:1 (< {minimum})"
