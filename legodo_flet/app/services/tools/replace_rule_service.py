# -*- coding: utf-8 -*-
"""ReplaceRuleService：用户级全局替换规则（内容净化）CRUD + 正文应用（M5，契约 §6）。

对标 Legado ``ReplaceRule`` 实体（全局替换规则表），区别于书源内嵌的
``read_config.titleReplaceRules / contentReplaceRules``：

- **数据**：``replace_rules`` 表（id/name/pattern/replacement/scope/is_regex/enabled）。
  ``scope`` 取 ``content``（正文，默认）/ ``title``（章节标题），对应 Legado
  ``isTitle``；``is_regex`` 对应 ``isRegex``（默认开）。
- **正文应用（壳内原位增强，红线）**：本服务在 on_start 时把一个 provider 注入
  ``app.services.engine.reading_engine`` 的全局替换规则钩子
  （``set_global_replace_rules_provider``）。``process_book_content`` /
  ``chapter_display_title`` 在**书源内嵌规则之后**再套用全局规则——两级叠加，
  顺序与 Legado ``ContentProcessor`` 对齐（书源内嵌先、全局后）。未注入钩子时
  （无服务环境/旧路径）reading_engine 行为逐字节不变，由 test_legado_compat 守门。
- **开关**：全局叠加是否生效由 SettingsService 键 ``replace.global.enabled``
  控制（默认开）；provider 在该键关闭时返回空列表，等同旧行为。
- **事件**：save/delete/set_enabled 后发 :class:`ReplaceRulesChangedEvent`，
  阅读链路收到后重洗正文缓存。

硬约束：零 flet import；依赖懒加载；跨平台纯 Python（正则求值复用 java_regex）。
"""
from __future__ import annotations

import json
import logging
import threading
import time

from app.core.events import ReplaceRulesChangedEvent
from app.core.lifecycle import Service
from app.core.storage import LockedConnection, StorageService

logger = logging.getLogger(__name__)

# 全局叠加开关的 SettingsService 键（契约 §6：新增全局替换应用设置，默认开）。
GLOBAL_ENABLED_KEY = "replace.global.enabled"


class ReplaceRuleService(Service):
    """全局替换规则的 CRUD + 正文两级叠加注入。"""

    name = "ReplaceRuleService"
    dependencies: tuple[type[Service], ...] = (StorageService,)

    def __init__(self, storage: StorageService | None = None,
                 settings=None) -> None:
        super().__init__()
        self._storage = storage
        self._settings = settings
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # 生命周期：注入/注销 reading_engine 全局规则钩子
    # ------------------------------------------------------------------ #
    async def on_start(self) -> None:
        if self._storage is None and self.hub is not None:
            self._storage = self.hub.require(StorageService)
        # settings 经 hub 取（可选）；取不到时全局叠加默认开（见 _global_on）。
        if self._settings is None and self.hub is not None:
            try:
                from app.core.settings import SettingsService
                self._settings = self.hub.require(SettingsService)
            except Exception:
                self._settings = None
        # 注入正文链路钩子（壳内原位增强）。
        from app.services.engine import reading_engine as _re
        _re.set_global_replace_rules_provider(self._provide_global_rules)
        logger.info("ReplaceRuleService 已启动（全局替换钩子已注入）")

    async def on_stop(self) -> None:
        from app.services.engine import reading_engine as _re
        _re.set_global_replace_rules_provider(None)

    def _conn(self) -> LockedConnection | None:
        return self._storage.db if self._storage is not None else None

    def _global_on(self) -> bool:
        """全局叠加开关：SettingsService 键，缺省视为开（默认开）。"""
        if self._settings is not None:
            try:
                return bool(self._settings.get(GLOBAL_ENABLED_KEY, True))
            except Exception:
                return True
        return True

    # ------------------------------------------------------------------ #
    # 正文链路注入面（reading_engine 钩子调用）
    # ------------------------------------------------------------------ #
    def _provide_global_rules(self, scope: str) -> list:
        """按 scope 返回启用中的全局规则 dict 列表（_apply_purify_rules 认识）。

        全局开关关闭时返回空列表——等同未叠加，书源内嵌行为不变。
        """
        if not self._global_on():
            return []
        conn = self._conn()
        if conn is None:
            return []
        with self._lock:
            rows = conn.execute(
                "SELECT pattern, replacement, is_regex FROM replace_rules"
                " WHERE enabled=1 AND scope=? ORDER BY id ASC",
                (str(scope),)).fetchall()
        return [
            {
                "pattern": r[0], "replacement": r[1],
                "isEnabled": True, "isRegex": bool(r[2]),
            }
            for r in rows
        ]

    # ------------------------------------------------------------------ #
    # 全局叠加开关（SettingsService 键，管理页那个总开关就是它）
    # ------------------------------------------------------------------ #
    async def global_enabled(self) -> bool:
        """全局替换当前是否参与正文叠加。"""
        return self._global_on()

    async def set_global_enabled(self, value: bool) -> bool:
        """开/关全局叠加并让阅读链路重洗（规则行不动，只停叠加）。"""
        if self._settings is None and self.hub is not None:
            try:
                from app.core.settings import SettingsService
                self._settings = self.hub.require(SettingsService)
            except Exception:
                self._settings = None
        if self._settings is None:
            raise RuntimeError("SettingsService 未就绪，无法保存全局替换开关")
        self._settings.set(GLOBAL_ENABLED_KEY, bool(value))
        self._publish(ReplaceRulesChangedEvent(ids=[]))
        return bool(value)

    # ------------------------------------------------------------------ #
    # CRUD（契约 §6：list_rules / save_rule / delete_rule / set_enabled）
    # ------------------------------------------------------------------ #
    async def list_rules(self) -> list[dict]:
        """列出全部规则（按 scope、id 排序），供管理视图渲染。"""
        conn = self._conn()
        if conn is None:
            return []
        with self._lock:
            rows = conn.execute(
                "SELECT id, name, pattern, replacement, scope, is_regex,"
                "       enabled, created_at, updated_at"
                " FROM replace_rules"
                " ORDER BY scope ASC, id ASC"
            ).fetchall()
        return [
            {
                "id": r[0], "name": r[1], "pattern": r[2],
                "replacement": r[3], "scope": r[4],
                "is_regex": bool(r[5]), "enabled": bool(r[6]),
                "created_at": float(r[7]), "updated_at": float(r[8]),
            }
            for r in rows
        ]

    async def save_rule(self, rule: dict) -> int:
        """新增或更新一条规则。

        ``rule`` 带 ``id`` 时更新该行；否则新增。返回规则 id。
        """
        rule = dict(rule or {})
        rid = rule.get("id")
        name = str(rule.get("name", "") or "")
        pattern = str(rule.get("pattern", "") or "")
        replacement = str(rule.get("replacement", "") or "")
        scope = str(rule.get("scope", "content") or "content")
        if scope not in ("content", "title"):
            scope = "content"
        is_regex = 1 if rule.get("is_regex", True) else 0
        enabled = 1 if rule.get("enabled", True) else 0
        now = time.time()

        conn = self._conn()
        if conn is None:
            raise RuntimeError("StorageService 未启动，无法保存替换规则")
        with self._lock:
            if rid:
                conn.execute(
                    "UPDATE replace_rules SET name=?, pattern=?,"
                    " replacement=?, scope=?, is_regex=?, enabled=?,"
                    " updated_at=? WHERE id=?",
                    (name, pattern, replacement, scope, is_regex,
                     enabled, now, int(rid)))
            else:
                cur = conn.execute(
                    "INSERT INTO replace_rules"
                    "(name, pattern, replacement, scope, is_regex, enabled,"
                    " created_at, updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (name, pattern, replacement, scope, is_regex, enabled,
                     now, now))
                rid = cur.lastrowid
        self._publish(ReplaceRulesChangedEvent(ids=[int(rid)]))
        return int(rid)

    async def delete_rule(self, rid: int) -> bool:
        """删除一条规则；返回是否真删了行。"""
        conn = self._conn()
        if conn is None:
            return False
        with self._lock:
            cur = conn.execute("DELETE FROM replace_rules WHERE id=?",
                               (int(rid),))
            changed = cur.rowcount > 0
        if changed:
            self._publish(ReplaceRulesChangedEvent(ids=[int(rid)]))
        return changed

    async def set_enabled(self, rid: int, enabled: bool) -> bool:
        """启用/停用一条规则；返回是否命中行。"""
        conn = self._conn()
        if conn is None:
            return False
        with self._lock:
            cur = conn.execute(
                "UPDATE replace_rules SET enabled=?, updated_at=?"
                " WHERE id=?",
                (1 if enabled else 0, time.time(), int(rid)))
            changed = cur.rowcount > 0
        if changed:
            self._publish(ReplaceRulesChangedEvent(ids=[int(rid)]))
        return changed

    # ------------------------------------------------------------------ #
    # 导入导出（WebDAV 备份成员 replaceRules.json；方法名即服务侧鸭子探测的契约）
    # ------------------------------------------------------------------ #
    async def export_rules_json(self) -> str:
        """导出全部规则为 JSON 数组（去掉本地 id/时间戳，跨设备可导入）。"""
        rows = await self.list_rules()
        return json.dumps(
            [{"name": r["name"], "pattern": r["pattern"],
              "replacement": r["replacement"], "scope": r["scope"],
              "is_regex": r["is_regex"], "enabled": r["enabled"]}
             for r in rows], ensure_ascii=False, indent=2)

    async def import_rules_json(self, text: str) -> tuple[int, list[str]]:
        """从 JSON 导入规则，按 (name, pattern) 跳过已有行；返回 (新增数, 告警)。"""
        try:
            data = json.loads(text)
        except Exception as exc:
            return 0, [f"JSON 解析失败: {exc}"]
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list):
            return 0, ["替换规则 JSON 必须是对象或对象数组"]
        existing = {(r["name"], r["pattern"]) for r in await self.list_rules()}
        errors: list[str] = []
        added = 0
        for item in data:
            if not isinstance(item, dict):
                errors.append("跳过非对象规则")
                continue
            pattern = str(item.get("pattern", "") or "")
            if not pattern.strip():
                errors.append("跳过无 pattern 的规则")
                continue
            if (str(item.get("name", "") or ""), pattern) in existing:
                continue
            try:
                await self.save_rule(item)
            except Exception as exc:
                errors.append(f"保存失败: {exc}")
                continue
            added += 1
        return added, errors

    # ------------------------------------------------------------------ #
    def _publish(self, event: ReplaceRulesChangedEvent) -> None:
        if self.bus is not None:
            self.bus.publish(event)
