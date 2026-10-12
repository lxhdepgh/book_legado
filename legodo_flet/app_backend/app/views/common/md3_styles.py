"""MD3 style constants for consistent component styling across views.

Design tokens aligned with Material Design 3 specification:
- Border radius: 12px (cards), 20px (buttons), 8px (inputs)
- Shadow elevations: following MD3 elevation scale (0-5)
- Spacing: 4dp grid system
- Typography: standard text styles
"""
from __future__ import annotations


# --------------------------------------------------------------------------- #
# Border radius — MD3 shape scale
# --------------------------------------------------------------------------- #
RADIUS_CARD = 12      # Cards, dialogs, elevated surfaces
RADIUS_BUTTON = 20    # Filled/tonal buttons (pill shape)
RADIUS_INPUT = 8      # Text fields, dropdowns, search boxes
RADIUS_CHIP = 8       # Filter chips, tags
RADIUS_SHEET = 28     # Bottom sheets, large modals


# --------------------------------------------------------------------------- #
# Shadow elevations — MD3 elevation scale (light mode values)
# --------------------------------------------------------------------------- #
# Format: (offset_x, offset_y, blur_radius, spread, color_opacity)
ELEVATION_0 = None                    # Flat / no shadow
ELEVATION_1 = (0, 1, 2, 0, 0.05)     # Resting state for cards
ELEVATION_2 = (0, 1, 3, 0, 0.08)     # Hover state
ELEVATION_3 = (0, 4, 6, -1, 0.1)     # Dragged / focused
ELEVATION_4 = (0, 6, 10, -2, 0.12)   # Modal overlays
ELEVATION_5 = (0, 8, 12, -2, 0.14)   # Navigation drawers


def make_shadow(elevation: int, color: str = "#000000") -> list[dict] | None:
    """Build Flet shadow list from MD3 elevation level.

    Parameters
    ----------
    elevation : int
        0-5, corresponding to MD3 elevation scale
    color : str
        Shadow color (default black; dark mode may use lighter opacity)

    Returns
    -------
    list[dict] | None
        Flet-compatible shadow spec or None for elevation 0
    """
    specs = {
        0: None,
        1: ELEVATION_1,
        2: ELEVATION_2,
        3: ELEVATION_3,
        4: ELEVATION_4,
        5: ELEVATION_5,
    }
    spec = specs.get(elevation)
    if spec is None:
        return None
    ox, oy, blur, spread, opacity = spec
    return [{"offset": (ox, oy), "blur_radius": blur, "spread_radius": spread,
             "color": f"{color}{int(opacity * 255):02x}"}]


# --------------------------------------------------------------------------- #
# Spacing — 4dp grid system
# --------------------------------------------------------------------------- #
SPACE_XS = 4    # Tight padding (icon margins)
SPACE_SM = 8    # Standard padding (input fields)
SPACE_MD = 16   # Section spacing
SPACE_LG = 24   # Card gaps, group spacing
SPACE_XL = 32   # Page margins


# --------------------------------------------------------------------------- #
# Component heights
# --------------------------------------------------------------------------- #
BUTTON_HEIGHT = 40          # Standard button height
INPUT_HEIGHT = 48           # Text field height
NAV_RAIL_WIDTH = 80         # Narrow rail (icons only)
NAV_RAIL_WIDE = 200         # Extended rail (icons + labels)
TAB_HEIGHT = 48             # Tab bar height


# --------------------------------------------------------------------------- #
# Opacity tokens (for disabled states, overlays)
# --------------------------------------------------------------------------- #
OPACITY_DISABLED = 0.38     # Disabled controls
OPACITY_OVERLAY = 0.4       # Modal backdrops
OPACITY_HOVER = 0.08        # Hover overlay on primary
