#!/usr/bin/env python3
"""MD3 theme smoke test: verify themes apply without crashing."""
import sys
from pathlib import Path

# Add legodo_flet to path (not project root)
_FLET_ROOT = Path(__file__).resolve().parent
if str(_FLET_ROOT) not in sys.path:
    sys.path.insert(0, str(_FLET_ROOT))


def test_theme_import():
    """Verify MD3 theme modules import correctly."""
    from app.themes.md3_theme import build_light_theme, build_dark_theme
    from app.views.common.md3_styles import RADIUS_CARD, ELEVATION_1
    from app.views.common.md3_components import make_card, make_filled_button

    # Verify constants are reasonable
    assert RADIUS_CARD == 12
    assert ELEVATION_1 is not None

    # Verify theme builders return valid Theme objects
    light = build_light_theme()
    dark = build_dark_theme()

    assert hasattr(light, "color_scheme")
    assert hasattr(dark, "color_scheme")
    assert light.color_scheme.primary == "#6750A4"
    assert dark.color_scheme.primary == "#D0BCFF"

    print("OK MD3 theme import test passed")


def test_component_factories():
    """Verify component factories create valid Flet controls."""
    import flet as ft
    from app.views.common.md3_components import (
        make_card,
        make_filled_button,
        make_text_field,
    )

    card = make_card(ft.Text("Test"))
    assert isinstance(card, ft.Container)
    assert card.border_radius == 12

    btn = make_filled_button("Click me")
    assert isinstance(btn, ft.FilledButton)
    assert btn.height == 40

    field = make_text_field(hint_text="Enter text")
    assert isinstance(field, ft.TextField)
    assert field.border_radius == 8
    assert field.height == 48

    print("OK Component factory test passed")


if __name__ == "__main__":
    try:
        test_theme_import()
        test_component_factories()
        print("\nAll MD3 theme tests passed OK")
    except Exception as e:
        print(f"\nTest failed: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)
