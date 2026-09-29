# -*- coding: utf-8 -*-
"""DictService：划词词典规则 CRUD + 划词释义请求（M5，契约 §7）。

对标 Legado 词典：用户配置若干"词典规则"，每条规则就是一个"目标为释义页"的
特殊书源——``url`` 为释义页模板（含 ``{{key}}`` 占位），``rules_json`` 存
legado 规则（ruleContent 等）。划词时：

1. 取第一条启用规则，把 ``{{key}}`` 替换为待查词；
2. **复用 RuleEngineService 网络栈抓释义页**（会话复用/UA/超时/cookie 语义一致）；
3. **复用 legado_rule_parser 体系解析释义正文**——不另写解析器。

数据：``dict_rules`` 表（id/name/url/rules_json/enabled）。

硬约束：零 flet import；依赖懒加载；跨平台。取词弹窗在视图层
（ReaderView 划词 → DictController → DictService.lookup），本服务不含 UI。
"""
from __future__ import annotations

import json
import logging
import threading
import time

from app.core.lifecycle import Service
from app.core.storage import LockedConnection, StorageService

logger = logging.getLogger(__name__)


class DictService(Service):
    """词典规则 CRUD + 划词释义抓取。"""

    name = "DictService"
    dependencies: tuple[type[Service], ...] = (StorageService,)

    def __init__(self, storage: StorageService | None = None,
                 rule_engine=None, fetcher=None) -> None:
        super().__init__()
        self._storage = storage
        self._engine_service = rule_engine
        # fetcher 注入面：async callable(url) -> str。None 时走 RuleEngineService
        # 网络栈；测试注入本地返回以离线跑（不联网）。
        self._fetcher = fetcher
        self._lock = threading.Lock()

    async def on_start(self) -> None:
        if self._storage is None and self.hub is not None:
            self._storage = self.hub.require(StorageService)
        if self._engine_service is None and self.hub is not None:
            try:
                from app.services.engine.rule_engine_service import (
                    RuleEngineService)
                self._engine_service = self.hub.require(RuleEngineService)
            except Exception:  # 阅读域未注册时只能靠注入 fetcher
                logger.info("DictService：RuleEngineService 未注册，"
                            "释义抓取依赖注入 fetcher")

    def _conn(self) -> LockedConnection | None:
        return self._storage.db if self._storage is not None else None

    # ------------------------------------------------------------------ #
    # CRUD（契约 §7：list_rules / save_rule / delete_rule）
    # ------------------------------------------------------------------ #
    async def list_rules(self) -> list[dict]:
        conn = self._conn()
        if conn is None:
            return []
        with self._lock:
            rows = conn.execute(
                "SELECT id, name, url, rules_json, enabled"
                " FROM dict_rules ORDER BY id ASC"
            ).fetchall()
        result = []
        for r in rows:
            try:
                rules_json = json.loads(r[3]) if r[3] else {}
            except json.JSONDecodeError:
                rules_json = {}
            result.append({
                "id": r[0], "name": r[1], "url": r[2],
                "rules": rules_json, "enabled": bool(r[4]),
            })
        return result

    async def save_rule(self, rule: dict) -> int:
        """新增或更新一条词典规则，返回 id。

        ``rule`` 字段：id(可选)/name/url/rules(dict, legado 规则)/enabled。
        """
        rule = dict(rule or {})
        rid = rule.get("id")
        name = str(rule.get("name", "") or "")
        url = str(rule.get("url", "") or "").strip()
        if not url:
            raise ValueError("词典规则 url 不能为空")
        rules_json = json.dumps(rule.get("rules", {}) or {},
                                ensure_ascii=False)
        enabled = 1 if rule.get("enabled", True) else 0
        now = time.time()
        conn = self._conn()
        if conn is None:
            raise RuntimeError("StorageService 未启动，无法保存词典规则")
        with self._lock:
            if rid:
                conn.execute(
                    "UPDATE dict_rules SET name=?, url=?, rules_json=?,"
                    " enabled=?, updated_at=? WHERE id=?",
                    (name, url, rules_json, enabled, now, int(rid)))
            else:
                cur = conn.execute(
                    "INSERT INTO dict_rules(name, url, rules_json, enabled,"
                    " created_at, updated_at) VALUES(?,?,?,?,?,?)",
                    (name, url, rules_json, enabled, now, now))
                rid = cur.lastrowid
        return int(rid)

    async def delete_rule(self, rid: int) -> bool:
        conn = self._conn()
        if conn is None:
            return False
        with self._lock:
            cur = conn.execute("DELETE FROM dict_rules WHERE id=?",
                               (int(rid),))
            return cur.rowcount > 0

    # ------------------------------------------------------------------ #
    # 导入导出（WebDAV 备份成员 dictRules.json；方法名即服务侧鸭子探测的契约）
    # ------------------------------------------------------------------ #
    async def export_rules_json(self) -> str:
        """导出全部词典规则为 JSON 数组（去掉本地 id/时间戳）。"""
        rows = await self.list_rules()
        return json.dumps(
            [{"name": r["name"], "url": r["url"], "rules": r["rules"],
              "enabled": r["enabled"]} for r in rows],
            ensure_ascii=False, indent=2)

    async def import_rules_json(self, text: str) -> tuple[int, list[str]]:
        """从 JSON 导入规则，按 url 跳过已有行；返回 (新增数, 告警)。"""
        try:
            data = json.loads(text)
        except Exception as exc:
            return 0, [f"JSON 解析失败: {exc}"]
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list):
            return 0, ["词典规则 JSON 必须是对象或对象数组"]
        conn = self._conn()
        existing: set[str] = set()
        if conn is not None:
            with self._lock:
                existing = {str(r[0] or "") for r in conn.execute(
                    "SELECT url FROM dict_rules").fetchall()}
        errors: list[str] = []
        added = 0
        for item in data:
            if not isinstance(item, dict):
                errors.append("跳过非对象规则")
                continue
            url = str(item.get("url", "") or "").strip()
            if not url:
                errors.append("跳过无 url 的规则")
                continue
            if url in existing:
                continue
            try:
                await self.save_rule(item)
            except Exception as exc:
                errors.append(f"保存失败: {exc}")
                continue
            existing.add(url)
            added += 1
        return added, errors

    # ------------------------------------------------------------------ #
    # 划词取义（契约 §7：lookup(word) -> str）
    # ------------------------------------------------------------------ #
    async def lookup(self, word: str) -> str:
        """划词查询释义，返回释义正文（失败返回空串）。

        把词典规则视为"目标为释义页"的特殊书源：URL 模板里的 ``{{key}}``
        替换为待查词后，复用 RuleEngineService 网络栈抓页，再用 legado
        规则体系抽出释义文本。无启用规则/抓取失败时返回空串（视图提示无释义）。
        """
        word = str(word or "").strip()
        if not word:
            return ""
        rule = self._first_enabled_rule()
        if rule is None:
            logger.info("DictService.lookup：无启用词典规则")
            return ""
        page_url = self._fill_url(rule["url"], word)
        try:
            html_body = await self._fetch(page_url)
        except Exception as exc:  # 抓取失败不抛给取词弹窗，回空串
            self.status.last_error = f"释义页抓取失败: {exc}"
            logger.warning("DictService.lookup 抓取失败 %s: %s", page_url, exc)
            return ""
        return self._extract_definition(html_body, rule.get("rules", {}))

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    def _first_enabled_rule(self) -> dict | None:
        conn = self._conn()
        if conn is None:
            return None
        with self._lock:
            row = conn.execute(
                "SELECT id, name, url, rules_json FROM dict_rules"
                " WHERE enabled=1 ORDER BY id ASC LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        try:
            rules = json.loads(row[3]) if row[3] else {}
        except json.JSONDecodeError:
            rules = {}
        return {"id": row[0], "name": row[1], "url": row[2], "rules": rules}

    @staticmethod
    def _fill_url(template: str, word: str) -> str:
        """URL 模板填词：``{{key}}`` 占位替换；无占位时把词拼到末尾。"""
        from urllib.parse import quote
        encoded = quote(word)
        if "{{key}}" in template:
            return template.replace("{{key}}", encoded)
        sep = "&" if "?" in template else "?"
        return f"{template}{sep}q={encoded}"

    async def _fetch(self, url: str) -> str:
        if self._fetcher is not None:
            return await self._fetcher(url)
        if self._engine_service is None:
            raise RuntimeError("未注入 fetcher 且 RuleEngineService 不可用")
        engine = await self._engine_service._get_engine(None)
        body, _ctype, _final = await engine._request(
            {"url": url, "method": "GET"})
        return body

    @staticmethod
    def _extract_definition(html_body: str, rules: dict) -> str:
        """用 legado 规则从释义页 HTML 抽出释义正文（复用规则解析器）。"""
        from app.services.engine.legado_rule_parser import (
            evaluate, parse_markup)

        rule_content = str(rules.get("ruleContent")
                           or rules.get("content") or "").strip()
        if not rule_content:
            # 无规则：退化取整页文本（视图层自行截取），保证有返回。
            from app.services.engine.legado_rule_parser import html_to_text
            return html_to_text(html_body)
        root = parse_markup(html_body)
        values = evaluate(root, rule_content, "html")
        return "\n".join(str(v) for v in values if str(v).strip())
