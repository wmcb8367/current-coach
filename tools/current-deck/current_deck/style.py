"""Visual themes.

'sailboss' (default) copies the SailBoss current-readings slide made for Luther's
coach boat (1 Nov 2026): black page, dark-green map panels with green hairline
borders, cyan letter-spaced kicker, lime "SailBoss" wordmark, arrows coloured by
current strength (cyan, amber, coral, lime), white tail dots and bold mono labels.
'm2x' swaps in the Current Coach app palette (slate-950, cyan-400).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

FONT_DIR = Path(__file__).parent / "fonts"


@dataclass(frozen=True)
class Theme:
    name: str
    page: str          # slide background
    panel: str         # map water
    land: str
    coast: str
    border: str        # panel borders
    box: str           # legend / callout background
    kicker: str        # small caps line above the title
    wordmark: str      # "SailBoss" top right, race-area box, playhead
    text: str          # titles, labels, scale bar
    muted: str
    faint: str
    strength: tuple    # 4 colours, weakest to strongest
    wordmark_text: str = "SailBoss"
    mono: str = "IBM Plex Mono"
    sans: str = "IBM Plex Sans"


THEMES = {
    "sailboss": Theme(
        name="sailboss", page="#000000", panel="#04140A", land="#1E2E24", coast="#2F4535",
        border="#1A4716", box="#0A120A", kicker="#55CFF5", wordmark="#28FE14",
        text="#F0F1E9", muted="#8DA38F", faint="#5F7361",
        strength=("#55CFF5", "#E8C547", "#F36B5C", "#28FE14"),
    ),
    "m2x": Theme(
        name="m2x", page="#020617", panel="#0B1224", land="#1C2537", coast="#2C3850",
        border="#1E2A40", box="#0D1323", kicker="#67E8F9", wordmark="#22D3EE",
        text="#ECF4FF", muted="#94A3B8", faint="#64748B",
        strength=("#67E8F9", "#FBBF24", "#F87171", "#34D399"),
        wordmark_text="Current Coach",
    ),
}


def register_fonts():
    """Make the bundled IBM Plex fonts available to matplotlib."""
    from matplotlib import font_manager

    for f in FONT_DIR.glob("*.ttf"):
        try:
            font_manager.fontManager.addfont(str(f))
        except Exception:  # pragma: no cover
            pass
