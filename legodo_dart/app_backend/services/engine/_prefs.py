# -*- coding: utf-8 -*-
"""M1 网络栈全局偏好桥接（§2.13 / §2.20 落点）。

核心资产引擎（rule_engine / legado_js）在发请求时需要读取全局偏好：
User-Agent、超时档位等。正常运行时这些偏好来自 SettingsService
（``app.core.settings``，由并行子代理实现）；在 core 尚未就位、或离线单测
环境下，软导入失败则回落到这里的硬编码默认值（与旧版逐字节一致），
保证核心资产行为不回归、测试不依赖外网或并行子代理。

RuleEngineService.start() 会把 SettingsService 的取值回填进本模块的进程级覆盖，
之后引擎读到的就是全局配置。
"""
from __future__ import annotations

from typing import Any

# 原硬编码默认 UA（rule_engine.py 与 legado_js.py 两处旧值完全一致）。
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/119.0.0.0 Safari/537.36"
)

# [M1-2.13] 超时三档默认值（近似 Legado connect 15s / read 60s / call 90s）。
DEFAULT_CONNECT_TIMEOUT = 15.0   # sock_connect：建立 TCP/TLS 连接的预算
DEFAULT_SOCK_READ_TIMEOUT = 60.0  # sock_read：两次读操作之间的间隔上限
DEFAULT_TOTAL_TIMEOUT = 90.0    # total：整次请求兜底预算

# 进程级覆盖（RuleEngineService 注入 / 测试用）。优先级高于 SettingsService。
_overrides: dict[str, Any] = {}


def get_setting(key: str, default: Any = None) -> Any:
    """读取全局偏好。

    顺序：进程级覆盖 > SettingsService.get_setting/get > default。
    任何一步异常都静默回退，绝不让偏好读取本身打断规则执行。
    """
    if key in _overrides:
        return _overrides[key]
    try:
        from app.core import settings as _core_settings  # type: ignore
    except Exception:
        return default
    getter = (getattr(_core_settings, "get_setting", None)
              or getattr(_core_settings, "get", None))
    if callable(getter):
        try:
            return getter(key, default)
        except Exception:
            return default
    return default


def set_override(key: str, value: Any) -> None:
    """RuleEngineService/测试向进程级覆盖写入一个偏好。"""
    _overrides[key] = value


def clear_override(key: str) -> None:
    """清除进程级覆盖（回到 SettingsService / 默认值）。"""
    _overrides.pop(key, None)


def user_agent() -> str:
    """当前生效的全局 UA（UA=="null" 哨兵由请求层自行处理，不在此删）。"""
    value = get_setting("userAgent", DEFAULT_USER_AGENT)
    return str(value if value is not None else DEFAULT_USER_AGENT)


def toc_count_words() -> bool:
    """[M2-2.8] 目录 updateTime 尾部"3240 字"拆进 chapter.wordCount 的全局开关。

    对齐 Legado ``AppConfigConfig.tocCountWords``（默认开）：开时把章节更新时间里
    形如"字数"的尾巴抽到 wordCount，剩余部分进 tag；关时整段进 tag。软读取经
    app.services.engine._prefs，默认 True。
    """
    value = get_setting("tocCountWords", True)
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "off", "no"}
    return bool(value)
