"""快捷键：键名规范化、组合键合成、冲突判定与动作清单（纯函数，core 零 flet §4.6）。

为什么单独一个模块：阅读器的键表原先只住在 ``reader_controller`` 里，界面没有任何
地方能改它（用户报"打开软件没找到按键设置的位置"）。要做一个能录键的设置页，
规范化与显示这两件事就必须**两边共用一份**——否则页面上显示 "PageDown"、
比较时用的是 "pagedown"，录进去的键位永远不会命中。

三件事各自有明确的形状：

* :func:`normalize_key` —— flet 的 ``KeyboardEvent.key`` 写法很野（``Arrow Left``
  / ``arrowleft`` / ``PageDown`` / ``KeyF`` / ``ControlLeft`` 都可能出现），
  统一压成小写规范名；
* :func:`chord_of` —— 把"按住 Ctrl 再按 H"合成一条 **ctrl+alt+shift+meta 顺序**
  的弦（modifier 单独按下的那一拍返回空串，它不成键位）；
* :func:`format_binding` —— 反过来给人看的写法。

**旧库里的单键绑定必须继续有效**：存过的是 ``left``/``pagedown`` 这种没有修饰
键的串，:func:`chord_of` 在无修饰键时给出的也正是这个串，所以历史偏好不用迁移。
"""
from __future__ import annotations

from dataclasses import dataclass

#: 修饰键本身：单独按下它们不成一个键位（录键时忽略，分发时也不能当动作）。
MODIFIER_ONLY: frozenset[str] = frozenset({"ctrl", "alt", "shift", "meta"})

#: 组合串里修饰键的固定顺序（存与比都按这个顺序，"ctrl+alt" 与 "alt+ctrl" 同值）。
MODIFIER_ORDER: tuple[str, ...] = ("ctrl", "alt", "shift", "meta")

#: flet 事件键名 → 规范键名（沿用阅读器旧别名表，缺一项就会"录上了不认"）。
KEY_ALIASES: dict[str, str] = {
    "arrow left": "left", "arrowleft": "left",
    "arrow right": "right", "arrowright": "right",
    "arrow up": "up", "arrowup": "up",
    "arrow down": "down", "arrowdown": "down",
    "page up": "pageup", "page down": "pagedown",
    " ": "space", ",": "comma", "keyf": "f",
    "keycomma": "comma", "esc": "escape",
    "control left": "ctrl", "control right": "ctrl",
    "controlleft": "ctrl", "controlright": "ctrl",
    "ctrl left": "ctrl", "ctrl right": "ctrl",
    "shift left": "shift", "shift right": "shift",
    "shiftleft": "shift", "shiftright": "shift",
    "alt left": "alt", "alt right": "alt",
    "altleft": "alt", "altright": "alt",
    "meta left": "meta", "meta right": "meta",
    "metaleft": "meta", "metaright": "meta",
    "windows left": "meta", "windowsright": "meta",
    "win left": "meta", "winright": "meta",
    # 显示写法念的是 "Win"，读回来还得是同一个修饰键（不列这条，"Win + P" 存一次
    # 就静默降级成裸 "p"——主键把修饰键顶掉）。
    "win": "meta",
    "backspace": "backspace", "delete": "del",
    "insert": "ins", "return": "enter", "enter": "enter",
    "tab": "tab", "quote": "'",
}

#: 规范键名 → 上屏写法。
_DISPLAY: dict[str, str] = {
    "ctrl": "Ctrl", "alt": "Alt", "shift": "Shift", "meta": "Win",
    "pageup": "PageUp", "pagedown": "PageDown", "escape": "Esc",
    "space": "空格", "comma": "逗号(,)", "up": "↑", "down": "↓",
    "left": "←", "right": "→", "home": "Home", "end": "End",
    "backspace": "Backspace", "del": "Delete", "ins": "Insert",
    "enter": "Enter", "tab": "Tab",
}

#: **显示写法的反向表**（只收非拉丁的那几条："逗号(,)"→comma、"空格"→space、"↑"→up）。
#: 页面上念出来的就是 :data:`_DISPLAY` 那一列，读不回同一个绑定＝"显示过一次再存库
#: 键位自己变了"（拉丁那几条 :func:`normalize_key` 压小写后本来就能对上，不用列）。
_DISPLAY_REVERSE: dict[str, str] = {
    value: key for key, value in _DISPLAY.items() if not value.isascii()}

#: 快捷键在 ``settings`` 表里的落点：阅读那组是 ``reader.shortcuts``（字典，
#: 阅读器快捷键偏好键（ReadingService.save_reader_preference 用），老板键已移除（Android 不支持）。
READER_PREF_KEY = "shortcuts"


def normalize_key(value: object) -> str:
    """flet 键名 → 规范键名（小写、去下划线、``KeyF``/``Digit3`` 折成单字符）。"""
    key = str(value or "").strip().lower().replace("_", " ").strip()
    if not key:
        return ""
    if len(key) == 5 and key.startswith("key "):
        key = key[-1]                       # "Key F" → "f"
    if len(key) == 4 and key.startswith("key") and key[3].isalnum():
        key = key[-1]                       # "KeyF" → "f"、"Digit3" 同理走下一条
    if len(key) == 6 and key.startswith("digit") and key[5].isdigit():
        key = key[-1]                       # "Digit3" → "3"
    return KEY_ALIASES.get(key, key)


def parse_binding(text: object) -> str:
    """用户写法/历史存量 → 规范组合串（``"Ctrl + Alt + H"`` → ``"ctrl+alt+h"``）。

    只按 ``+`` 拆段：``"page_down"`` 与 ``"Page Down"`` 是**同一段内部**的写法，
    交给 :func:`normalize_key` 收（按下划线拆会把 "page_down" 当成两段，
    绑定就错落成 "down"）。顺序按 :data:`MODIFIER_ORDER` 重排，所以
    ``"alt+ctrl+h"`` 与 ``"ctrl+alt+h"`` 同值。段内先认 :data:`_DISPLAY_REVERSE`，
    所以 :func:`format_binding` 念出来的写法能原样读回。
    """
    parts = [p.strip() for p in str(text or "").split("+") if p.strip()]
    if not parts:
        return ""
    mods: list[str] = []
    main = ""
    for part in parts:
        token = _DISPLAY_REVERSE.get(part) or normalize_key(part)
        if token in MODIFIER_ONLY:
            if token not in mods:
                mods.append(token)
        elif token:
            main = token
    if not main:
        # 只按了修饰键的组合不认（历史存里可能有"ctrl"这种半截绑定）。
        return ""
    ordered = [m for m in MODIFIER_ORDER if m in mods]
    return "+".join([*ordered, main])


def chord_of(event: object) -> str:
    """一次按键事件 → 组合串；纯修饰键那一拍返回空串（调用方据此忽略）。"""
    main = normalize_key(getattr(event, "key", ""))
    if not main or main in MODIFIER_ONLY:
        return ""
    mods = [name for name in ("ctrl", "alt", "shift", "meta")
            if bool(getattr(event, name, False))]
    return "+".join([*mods, main])


def format_binding(binding: object) -> str:
    """组合串 → 给人看的写法（``"ctrl+alt+pageup"`` → ``"Ctrl + Alt + PageUp"``）。"""
    parts = [p for p in str(binding or "").split("+") if p]
    if not parts:
        return "未设置"
    return " + ".join(_DISPLAY.get(p, p.upper() if len(p) == 1 else p)
                      for p in parts)


@dataclass(frozen=True)
class ShortcutAction:
    """一个可绑定的动作：id 就是存在偏好里的键名，别处不再散着写字符串。"""

    id: str
    label: str
    hint: str = ""


#: 阅读器动作（顺序即设置页展示顺序；id 与 ``reader.shortcuts`` 字典的键一致）。
READER_ACTIONS: tuple[ShortcutAction, ...] = (
    ShortcutAction("page_down", "翻下一页", "正文向下翻一屏"),
    ShortcutAction("page_up", "翻上一页", "正文向上翻一屏"),
    ShortcutAction("next", "下一章", "跳到目录里的后一章"),
    ShortcutAction("previous", "上一章", "跳到目录里的前一章"),
    ShortcutAction("directory", "章节目录", "打开/收起章节列表"),
    ShortcutAction("settings", "阅读设置", "打开字号/行距等设置面板"),
    ShortcutAction("close", "退出阅读", "离开正文页，回上一个位置"),
)

#: 阅读器默认键位（对齐旧 ``ReadingView._load_reader_preferences``）。
DEFAULT_READER_SHORTCUTS: dict[str, str] = {
    "previous": "left", "next": "right",
    "page_up": "pageup", "page_down": "pagedown",
    "directory": "f", "settings": "comma", "close": "escape",
}


def default_reader_shortcuts() -> dict[str, str]:
    """默认键位的**副本**（调用方改不动模块字典）。"""
    return dict(DEFAULT_READER_SHORTCUTS)


def merge_reader_shortcuts(saved: object) -> dict[str, str]:
    """存量字典 + 默认值 → 只含已知动作的完整绑定表。

    未知键名丢掉（旧版本残留/手改库都不该占着冲突判定的位置）；已知动作没存过
    的补默认值，否则 ``shortcuts["next"]`` 这种取法会在缺键时 KeyError。
    """
    merged = default_reader_shortcuts()
    if isinstance(saved, dict):
        for name, binding in saved.items():
            if name in merged:
                merged[name] = parse_binding(binding)
    return merged


def action_for(binding: str, bindings: dict[str, str]) -> str:
    """组合串 → 动作 id：表里**第一个**命中的；空串永不命中。

    撞键时按表内顺序取第一个（:func:`conflicts` 会把它显示成冲突行），
    这样至少行为确定，不会随字典插入顺序变。空输入在开头就 return，
    所以库里的空绑定（"未设置"）根本走不到比较那一步。
    """
    if not binding:
        return ""
    for action_id, key in bindings.items():
        if key == binding:
            return action_id
    return ""


def conflicts(bindings: dict[str, str]) -> dict[str, list[str]]:
    """动作 id → 与它撞了同一个键位的其它动作 id（按展示顺序，不含自身）。

    空绑定（"未设置"）不参与撞键：允许用户把不想用的动作清干净。
    """
    used: dict[str, list[str]] = {}
    for action_id, binding in bindings.items():
        if not binding:
            continue
        used.setdefault(binding, []).append(action_id)
    return {action_id: [other for other in peers if other != action_id]
            for binding, peers in used.items()
            for action_id in peers if len(peers) > 1}


# ========== 老板键常量（G4-E 后保留，供 BossKeyService 使用）==========
#: 老板键启用开关偏好键名
BOSS_KEY_ENABLED = "bosskey.enabled"

#: 老板键快捷键偏好键名
BOSS_KEY_SHORTCUT = "bosskey.shortcut"

#: 默认老板键组合
DEFAULT_BOSS_KEY = "ctrl+alt+h"

#: 老板键动作定义
BOSS_ACTION = ShortcutAction(
    "boss_key",
    "老板键",
    "立刻藏起整个窗口，点托盘图标回来"
)
