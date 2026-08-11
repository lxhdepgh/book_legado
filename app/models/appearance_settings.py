"""Serializable appearance settings shared by the UI and theme service."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


VALID_MODES = {"system", "light", "dark", "eink"}


@dataclass
class ReaderVariant:
    background: str
    text: str


@dataclass
class ReaderSettings:
    theme_id: str = "follow_app"
    variants: dict[str, ReaderVariant] = field(default_factory=lambda: {
        "light": ReaderVariant("#FAF9F6", "#242424"),
        "dark": ReaderVariant("#121212", "#D8D8D8"),
        "eink": ReaderVariant("#FFFFFF", "#000000"),
    })
    font_family: str = "system"
    line_height: str = "standard"
    paragraph_spacing: str = "medium"
    page_margin: str = "standard"
    image_background: str = "follow_reader"


@dataclass
class AppearanceSettings:
    version: int = 2
    mode: str = "system"
    previous_mode: str = "system"
    skin_id: str = "classic_blue"
    custom_accent: str | None = None
    reader: ReaderSettings = field(default_factory=ReaderSettings)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Any, valid_skins: set[str]) -> "AppearanceSettings":
        if not isinstance(value, dict):
            raise ValueError("外观配置必须是 JSON 对象")
        mode = str(value.get("mode", "system"))
        previous_mode = str(value.get("previous_mode", "system"))
        skin_id = str(value.get("skin_id", "classic_blue"))
        if mode not in VALID_MODES:
            mode = "system"
        if previous_mode not in VALID_MODES - {"eink"}:
            previous_mode = "system"
        if skin_id not in valid_skins:
            skin_id = "classic_blue"

        reader_raw = value.get("reader") if isinstance(value.get("reader"), dict) else {}
        reader = ReaderSettings()
        variants_raw = reader_raw.get("variants", {})
        if isinstance(variants_raw, dict):
            for name in ("light", "dark", "eink"):
                variant = variants_raw.get(name)
                if isinstance(variant, dict):
                    background = str(variant.get("background", ""))
                    text = str(variant.get("text", ""))
                    if _is_hex_color(background) and _is_hex_color(text):
                        reader.variants[name] = ReaderVariant(background, text)
        for key in ("theme_id", "font_family", "line_height",
                    "paragraph_spacing", "page_margin", "image_background"):
            if key in reader_raw and isinstance(reader_raw[key], str):
                setattr(reader, key, reader_raw[key])
        accent = value.get("custom_accent")
        if accent is not None and not _is_hex_color(str(accent)):
            accent = None
        return cls(mode=mode, previous_mode=previous_mode, skin_id=skin_id,
                   custom_accent=accent, reader=reader)


def _is_hex_color(value: str) -> bool:
    value = value.strip()
    if not value.startswith("#") or len(value) not in (7, 9):
        return False
    try:
        int(value[1:], 16)
        return True
    except ValueError:
        return False
