"""Built-in application skins with explicit light and dark variants."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Palette:
    primary: str
    on_primary: str
    primary_container: str
    on_primary_container: str
    surface: str
    surface_container: str
    on_surface: str
    on_surface_variant: str
    outline: str


@dataclass(frozen=True)
class BuiltinSkin:
    id: str
    name: str
    description: str
    light: Palette
    dark: Palette


def _skin(skin_id: str, name: str, description: str, primary: str,
          dark_primary: str, light_container: str, dark_container: str) -> BuiltinSkin:
    return BuiltinSkin(
        skin_id, name, description,
        Palette(primary, "#FFFFFF", light_container, "#101828", "#FFFBFF",
                "#F2F0F4", "#1D1B20", "#49454F", "#79747E"),
        Palette(dark_primary, "#102018", dark_container, "#E7F1EA", "#141218",
                "#211F26", "#E6E1E5", "#CAC4D0", "#938F99"),
    )


BUILTIN_SKINS = {
    skin.id: skin for skin in (
        _skin("classic_blue", "经典蓝", "稳定、中性的默认皮肤", "#3F51B5",
              "#B8C4FF", "#DEE3FF", "#303B6D"),
        _skin("emerald_green", "翡翠绿", "柔和，适合长时间使用", "#2E7D6B",
              "#82D5BD", "#B8F1DF", "#175344"),
        _skin("warm_amber", "暖琥珀", "温暖、低刺激", "#9A6700",
              "#F0C36B", "#FFE2A8", "#5A4315"),
        _skin("elegant_purple", "雅致紫", "轻松而有辨识度", "#6D5BD0",
              "#C9BFFF", "#E7DEFF", "#483D79"),
        _skin("ink_gray", "水墨灰", "低饱和的管理界面", "#4F5B62",
              "#BBC7CD", "#DDE5E8", "#39454B"),
    )
}


EINK_PALETTE = Palette(
    "#000000", "#FFFFFF", "#FFFFFF", "#000000", "#FFFFFF", "#FFFFFF",
    "#000000", "#202020", "#000000")
