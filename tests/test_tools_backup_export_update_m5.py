"""M5 工具域（本行 Agent A）测试：WebDavService / BookExportService / UpdateService。

≥9 条，全程离线不联网：

- WebDAV 客户端以"本地目录当远端"的假客户端替代真实 PROPFIND/PUT/GET；
- 备份-导出-恢复链路经该假客户端闭环；恢复前强制校验 hash，篡改包被拒且不写入；
- epub 导出产物用 ebooklib 读回；txt 产物直接读文本；
- 更新检查用注入的 http_get_json/http_download stub；版本解析为纯函数。
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import shutil
import tempfile
import time
import zipfile
from pathlib import Path

import pytest

from app.core.bus import EventBus
from app.core.engine_worker import engine_hub
from app.core.events import (BackupFinishedEvent, BackupProgressEvent,
                             ExportProgressEvent, UpdateAvailableEvent)
from app.core.storage import StorageService
from app.services.engine.reading_engine import ChapterContent, ContentBlock
from app.services.engine.rule_engine import ChapterItem
from app.services.reading_cache import ReadingCache
from app.services.tools.book_export_service import BookExportService
from app.services.tools.update_service import UpdateService, UpdateInfo, _parse_release, _version_tuple
from app.services.tools.web_dav_service import WebDavService


# --------------------------------------------------------------------------- #
# 测试替身
# --------------------------------------------------------------------------- #
class DirDavClient:
    """用本地目录模拟 WebDAV 远端（实现 list/download/upload/mkdir）。"""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    async def list(self, path: str = "") -> list[dict]:
        d = self.root / path
        out: list[dict] = []
        if d.is_dir():
            for p in sorted(d.iterdir()):
                rel = str(Path(path) / p.name).replace("\\", "/")
                out.append({
                    "name": p.name,
                    "path": "/" + rel,
                    "size": p.stat().st_size,
                    "modified": p.stat().st_mtime,
                    "is_dir": p.is_dir(),
                })
        return out

    async def download(self, path: str) -> bytes:
        return (self.root / path).read_bytes()

    async def upload(self, path: str, data: bytes) -> None:
        f = self.root / path
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(data)

    async def mkdir(self, path: str) -> None:
        (self.root / path).mkdir(parents=True, exist_ok=True)


class FakeSource:
    """鸭子类型 SourceService：满足导出/导入/书架读写的最小面。"""

    def __init__(self, sources=None, bookshelf=None) -> None:
        self.sources = list(sources or [])
        self.bookshelf = list(bookshelf or [])
        self.saved_bookshelf = None
        self.export_calls = 0        # 记"有没有被导出过"（裁剪备份对象时用得上）

    async def export_sources_json(self) -> str:
        self.export_calls += 1
        return json.dumps(self.sources, ensure_ascii=False)

    async def import_sources_json(self, text: str):
        data = json.loads(text)
        self.sources = data if isinstance(data, list) else [data]
        return len(self.sources), []

    def save_bookshelf(self) -> None:
        self.saved_bookshelf = list(self.bookshelf)


class FakeSettings:
    """内存 SettingsService 替身。"""

    def __init__(self) -> None:
        self.d: dict = {}

    def get(self, key, default=None):
        return self.d.get(key, default)

    def set(self, key, value) -> None:
        self.d[key] = value


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture()
def env(tmp_path, monkeypatch):
    """真实 StorageService（tmp 数据根）+ EventBus + 备份/导出服务。"""
    root = Path(tempfile.mkdtemp(prefix="m5tools_"))
    monkeypatch.setenv("LEGADO_BOOKSOURCE_DATA_DIR", str(root / "data"))
    monkeypatch.setenv("LEGADO_BOOKSOURCE_CACHE_DIR", str(root / "cache"))

    engine = engine_hub()
    storage = StorageService()
    asyncio.run(storage.start())
    bus = EventBus(engine=engine)

    dav_root = root / "dav"
    dav = DirDavClient(dav_root)

    captured: list = []

    async def _on_backup(ev):
        captured.append(ev)

    bus.subscribe(BackupFinishedEvent, _on_backup)

    try:
        yield type("E", (), {
            "root": root, "storage": storage, "bus": bus, "dav": dav,
            "dav_root": dav_root, "captured": captured,
        })
    finally:
        asyncio.run(storage.stop())
        shutil.rmtree(root, ignore_errors=True)


def _drain(bus, seconds: float = 2.0) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        time.sleep(0.02)


def _make_export_book_cache(cache_root: Path) -> ReadingCache:
    """构造一个含 2 章已缓存内容（含 1 张内联图片）的 ReadingCache。"""
    cache = ReadingCache(cache_root)
    chapters = [
        ChapterItem(name="第一章", url="http://src/book/c1", index=0),
        ChapterItem(name="第二卷", url="", index=1, is_volume=True),  # 卷标题，跳过
        ChapterItem(name="第二章", url="http://src/book/c2", index=2),
    ]
    cache.save_catalog("src1", "book1", chapters)

    png = _png_bytes()
    cache.save_content("src1", "book1", "http://src/book/c1", ChapterContent(
        blocks=[ContentBlock("text", "第一章正文 你好世界")], title="第一章"))
    cache.save_content("src1", "book1", "http://src/book/c2", ChapterContent(
        blocks=[ContentBlock("text", "第二章正文 第二段落"),
                 ContentBlock("image", png)], title="第二章"))
    return cache


def _png_bytes() -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (200, 100, 50)).save(buf, format="PNG")
    return buf.getvalue()


def _make_multi_chapter_cache(cache_root: Path, count: int = 6,
                              cached: int | None = None) -> ReadingCache:
    """``count`` 章目录（末尾一个卷标题），其中 ``cached`` 章真有正文。

    缓存数**故意少于**目录数：只有一本"全缓存"的书，测不出进度分母到底取的
    哪个数（取目录条数的话，进度条永远停在 4/6）。
    """
    cache = ReadingCache(cache_root)
    chapters = [ChapterItem(name=f"第{i + 1}章", url=f"http://src/book/c{i}",
                            index=i) for i in range(count)]
    chapters.append(ChapterItem(name="第二卷", url="", index=count, is_volume=True))
    cache.save_catalog("src1", "book1", chapters)
    for index, item in enumerate(chapters[:count if cached is None else cached]):
        cache.save_content("src1", "book1", item.url, ChapterContent(
            blocks=[ContentBlock("text", f"第{index + 1}章正文 内容")],
            title=item.name))
    return cache


# --------------------------------------------------------------------------- #
# 1. WebDAV 客户端 stub：本地上传/列目录/下载闭环
# --------------------------------------------------------------------------- #
def test_webdav_client_stub_roundtrip(env):
    async def run():
        await env.dav.upload("legado/backup/a.zip", b"PK\x03\x04 fake")
        entries = await env.dav.list("legado/backup")
        await env.dav.download("legado/backup/a.zip")
        return entries
    entries = asyncio.run(run())
    assert [e["name"] for e in entries] == ["a.zip"]


# --------------------------------------------------------------------------- #
# 2. 备份 → list_remote 能看到 zip
# --------------------------------------------------------------------------- #
def test_backup_lists_remote(env):
    src = FakeSource(
        sources=[{"bookSourceUrl": "u1", "bookSourceName": "源一"}],
        bookshelf=[{"url": "book1", "source_url": "src1", "name": "测试书"}])
    svc = WebDavService(source_service=src, storage=env.storage,
                        client=env.dav, settings=FakeSettings())
    svc.bus = env.bus
    result = asyncio.run(svc.backup())
    assert result.ok, result.detail
    assert result.size > 0
    remote = asyncio.run(svc.list_remote())
    assert len(remote) == 1
    assert remote[0]["name"].endswith(".zip")


# --------------------------------------------------------------------------- #
# 3. 备份-恢复完整链路（本地目录当远端）
# --------------------------------------------------------------------------- #
def test_backup_restore_chain(env):
    src = FakeSource(
        sources=[{"bookSourceUrl": "u1", "bookSourceName": "源一"},
                 {"bookSourceUrl": "u2", "bookSourceName": "源二"}],
        bookshelf=[{"url": "book1", "source_url": "src1", "name": "测试书"}])
    svc = WebDavService(source_service=src, storage=env.storage,
                        client=env.dav, settings=FakeSettings())
    svc.bus = env.bus
    result = asyncio.run(svc.backup())
    assert result.ok

    # 清空本地，模拟换机
    src.sources = []
    src.bookshelf = []
    remote = asyncio.run(svc.list_remote())
    assert len(remote) == 1

    restored = asyncio.run(svc.restore(remote[0]["path"].lstrip("/"), confirm=True))
    assert restored.ok, restored.detail
    assert len(src.sources) == 2
    assert len(src.bookshelf) == 1
    assert src.bookshelf[0]["name"] == "测试书"
    assert src.saved_bookshelf is not None  # 已落盘


# --------------------------------------------------------------------------- #
# 4. 恢复拒绝被篡改的备份包（hash 不匹配 → 不写入）
# --------------------------------------------------------------------------- #
def test_restore_rejects_tampered_zip(env):
    src = FakeSource(
        sources=[{"bookSourceUrl": "u1"}],
        bookshelf=[{"url": "book1", "source_url": "src1", "name": "测试书"}])
    svc = WebDavService(source_service=src, storage=env.storage,
                        client=env.dav, settings=FakeSettings())
    svc.bus = env.bus
    result = asyncio.run(svc.backup())
    assert result.ok

    # 重新打一个 hash 不匹配的包：成员内容被改但 manifest 仍指向旧 hash
    bad = io.BytesIO()
    with zipfile.ZipFile(bad, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("meta.json", json.dumps({
            "format": "legado-desktop-backup", "version": 1,
            "manifest": {"bookSources.json": "0" * 64}}))
        zf.writestr("bookSources.json", json.dumps([{"hacked": True}]))
    asyncio.run(env.dav.upload("legado/backup/bad.zip", bad.getvalue()))

    before = list(src.sources)
    # confirm=True：不确认的话会在确认闸上直接返回，这条用例就什么都没校验到
    restored = asyncio.run(svc.restore("legado/backup/bad.zip", confirm=True))
    assert not restored.ok
    assert "校验" in restored.detail or "哈希" in restored.detail
    assert src.sources == before  # 未写入


# --------------------------------------------------------------------------- #
# 5. 恢复拒绝缺 meta.json 的包
# --------------------------------------------------------------------------- #
def test_restore_rejects_missing_meta(env):
    src = FakeSource(sources=[{"bookSourceUrl": "u1"}], bookshelf=[])
    svc = WebDavService(source_service=src, storage=env.storage,
                        client=env.dav, settings=FakeSettings())
    svc.bus = env.bus
    bad = io.BytesIO()
    with zipfile.ZipFile(bad, "w") as zf:
        zf.writestr("bookSources.json", "[]")
    asyncio.run(env.dav.upload("legado/backup/nometa.zip", bad.getvalue()))
    restored = asyncio.run(svc.restore("legado/backup/nometa.zip", confirm=True))
    assert not restored.ok


# --------------------------------------------------------------------------- #
# 5b. 恢复的确认闸：没 confirm 连下载都不做
# --------------------------------------------------------------------------- #
class _NeverTouchedClient:
    """任何远端动作都不该发生（确认闸挡在下载之前，不是"下载完再问"）。

    记 ``calls`` 而不是只抛错：抛错会被服务的 ``except`` 吞成"下载失败"，
    结果一样是 ok=False，光看 ok 分不出"闸挡住了"和"下载炸了"。
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def download(self, path):
        self.calls.append(f"download:{path}")
        raise AssertionError("未确认的恢复不该下载备份包")

    async def list(self, path=""):
        self.calls.append(f"list:{path}")
        raise AssertionError("未确认的恢复不该列目录")


def _svc(env, src):
    svc = WebDavService(source_service=src, storage=env.storage,
                        client=env.dav, settings=FakeSettings())
    svc.bus = env.bus
    return svc


def _zip_bytes(members: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    manifest = {name: hashlib.sha256(text.encode("utf-8")).hexdigest()
                for name, text in members.items()}
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("meta.json", json.dumps({
            "format": "legado-desktop-backup", "version": 1,
            "kind": "all", "manifest": manifest}))
        for name, text in members.items():
            zf.writestr(name, text)
    return buf.getvalue()


def test_restore_without_confirm_never_downloads(env):
    src = FakeSource(sources=[{"bookSourceUrl": "u1"}], bookshelf=[])
    client = _NeverTouchedClient()
    svc = WebDavService(source_service=src, storage=env.storage,
                        client=client, settings=FakeSettings())
    svc.bus = env.bus
    restored = asyncio.run(svc.restore("legado/backup/whatever.zip"))
    assert not restored.ok
    assert client.calls == [], client.calls              # 闸在下载之前，不是下载失败
    assert "需确认后才执行" in restored.detail, restored.detail
    assert src.sources == [{"bookSourceUrl": "u1"}]     # 本地什么都没动
    assert restored.snapshot == ""


def test_restore_leaves_a_snapshot_of_the_pre_restore_state(env):
    """留底留的是**覆盖前**的现状：恢复完才发现选错包时，那份现状还找得回来。"""
    src = FakeSource(sources=[{"bookSourceUrl": "u1"}, {"bookSourceUrl": "u2"}])
    svc = _svc(env, src)
    assert asyncio.run(svc.backup()).ok
    remote = asyncio.run(svc.list_remote())[0]["path"].lstrip("/")

    # 备份包传上去之后本地又变了：恢复要覆盖的是"又变了"的这一份
    src.sources = [{"bookSourceUrl": "local-only", "bookSourceName": "恢复前的本地源"}]
    restored = asyncio.run(svc.restore(remote, confirm=True))
    assert restored.ok, restored.detail

    snapshot = Path(restored.snapshot)
    assert snapshot.is_dir(), restored.snapshot
    saved = json.loads((snapshot / "bookSources.json").read_text(encoding="utf-8"))
    assert [s["bookSourceUrl"] for s in saved] == ["local-only"], saved
    assert [s["bookSourceUrl"] for s in src.sources] == ["u1", "u2"]
    assert str(snapshot) in restored.detail             # 结果行报出留底在哪


def test_snapshot_covers_only_members_the_package_replaces(env):
    """包里没带的成员不为它留底：只恢复书源时不必把书架现状也抄一遍。"""
    src = FakeSource(sources=[{"bookSourceUrl": "local"}],
                     bookshelf=[{"url": "b1", "source_url": "s", "name": "n"}])
    svc = _svc(env, src)
    text = json.dumps([{"bookSourceUrl": "from-remote"}], ensure_ascii=False)
    asyncio.run(env.dav.upload("legado/backup/only-sources.zip",
                               _zip_bytes({"bookSources.json": text})))
    restored = asyncio.run(svc.restore("legado/backup/only-sources.zip", confirm=True))
    assert restored.ok, restored.detail
    names = sorted(p.name for p in Path(restored.snapshot).iterdir())
    assert names == ["bookSources.json"], names


def test_restore_still_runs_without_a_snapshot_dir(env):
    """留底是保险丝不是关卡：storage 拿不到也得照样恢复，别变成"点了没反应"。"""
    src = FakeSource(sources=[{"bookSourceUrl": "u1"}])
    svc = WebDavService(source_service=src, storage=None,
                        client=env.dav, settings=FakeSettings())
    svc.bus = env.bus
    text = json.dumps([{"bookSourceUrl": "from-remote"}], ensure_ascii=False)
    asyncio.run(env.dav.upload("legado/backup/plain.zip",
                               _zip_bytes({"bookSources.json": text})))
    restored = asyncio.run(svc.restore("legado/backup/plain.zip", confirm=True))
    assert restored.ok, restored.detail
    assert restored.snapshot == ""
    assert [s["bookSourceUrl"] for s in src.sources] == ["from-remote"]


# --------------------------------------------------------------------------- #
# 5c. 逐阶段进度：备份/恢复的每一步都要在动手*之前*就说出来
# --------------------------------------------------------------------------- #
class _SilentBus:
    """同步记账的总线替身。

    真 :class:`EventBus` 的投递是异步的（publish 把处理器 submit 到引擎循环），
    这里要证的偏偏是"哪一拍先发生"——用真总线既得轮询等待，顺序 regress 了也未必看得见。
    """

    def __init__(self) -> None:
        self.events: list = []

    def subscribe(self, event_type, handler):
        return lambda: None

    def publish(self, event):
        self.events.append(event)
        return []


class _BrokenBus:
    """**进度**事件一发就炸的总线：进度不该把备份一起带走。

    只挡 BackupProgressEvent：收尾的 BackupFinishedEvent 是契约（成/败必须报出去），
    传不出去属于真失败，不该被这里一起吞掉。
    """

    def publish(self, event):
        if isinstance(event, BackupProgressEvent):
            raise RuntimeError("事件环已经关了")
        return []


def _backup_service(env, src, bus=None) -> WebDavService:
    """真 StorageService + 本地目录假远端的 WebDavService（bus=None 表示没总线）。"""
    svc = WebDavService(source_service=src, storage=env.storage,
                        client=env.dav, settings=FakeSettings())
    svc.bus = bus
    return svc


def _stages(bus) -> list:
    return [e for e in bus.events if isinstance(e, BackupProgressEvent)]


def test_backup_publishes_progress_for_each_step_it_runs(env):
    """备份全程十几个动作，返回值却只在最后才有：中途必须靠事件说话。"""
    src = FakeSource(sources=[{"bookSourceUrl": "u1"}],
                     bookshelf=[{"url": "b1", "source_url": "s", "name": "n"}])
    bus = _SilentBus()
    svc = _backup_service(env, src, bus)
    assert asyncio.run(svc.backup()).ok

    stages = _stages(bus)
    text = " | ".join(s.stage for s in stages)
    # 报的是中文名不是文件名：bookShelf.json 不等于"书架"
    assert "正在导出书源…" in text, text
    assert "正在上传" in text, text
    assert ".json" not in text, text
    # 校验那一圈是有条数的：done 逐格走到 total
    counted = [s for s in stages if s.total]
    assert [s.done for s in counted] == list(range(1, len(counted) + 1)), counted
    assert {s.total for s in counted} == {3}, counted      # 书源/书架/设置三个成员
    assert all("校验" in s.stage for s in counted), counted
    # 收尾仍然是那条 BackupFinishedEvent（进度不能把它顶掉）
    assert isinstance(bus.events[-1], BackupFinishedEvent), bus.events[-1]
    assert bus.events[-1].ok and bus.events[-1].kind == "all"


def test_partial_backup_progress_covers_only_what_it_exports(env):
    """只备份书架时不许冒出"正在导出书源"：那是用户没点过的东西。

    这条同时钉住服务侧的省工：以前 ``_collect_members`` 不管选什么都把整库序列化
    一遍再丢掉，几百条源时既慢又谎报进度。
    """
    src = FakeSource(sources=[{"bookSourceUrl": "u1"}] * 3,
                     bookshelf=[{"url": "b1", "source_url": "s", "name": "n"}])
    bus = _SilentBus()
    svc = _backup_service(env, src, bus)
    result = asyncio.run(svc.backup("bookshelf"))
    assert result.ok, result.detail
    assert svc._source.export_calls == 0, "没备份书源却把书源表导出了"
    text = " | ".join(s.stage for s in _stages(bus))
    assert "书架" in text, text
    for other in ("书源", "设置", "词典"):
        assert other not in text, f"只备份书架的进度里冒出了{other}：{text}"
    assert [s.total for s in _stages(bus) if s.total] == [1], "校验圈不是恰好一个成员"
    assert "书源 3 条" not in result.detail, result.detail


def test_progress_publishing_never_breaks_the_backup(env):
    """进度是装饰：总线炸了也要把备份做完，而不是回一句"操作失败"。"""
    src = FakeSource(sources=[{"bookSourceUrl": "u1"}])
    svc = _backup_service(env, src, _BrokenBus())
    result = asyncio.run(svc.backup())
    assert result.ok, result.detail
    remote = asyncio.run(svc.list_remote())
    assert len(remote) == 1


def test_backup_without_a_bus_still_completes(env):
    src = FakeSource(sources=[{"bookSourceUrl": "u1"}])
    svc = _backup_service(env, src, None)
    assert asyncio.run(svc.backup()).ok


def test_restore_progress_outruns_the_write(env):
    """恢复的进度必须发生在落库**之前**：收尾才一次性报进度等于没有进度。

    每一拍顺手记下当时本地有几条源——下载/校验/留底那几拍发生时本地还得是空的，
    只有"正在恢复书源"之后才变成 2 条。
    """
    src = FakeSource(sources=[{"bookSourceUrl": "u1"}, {"bookSourceUrl": "u2"}],
                     bookshelf=[])
    svc = _svc(env, src)
    assert asyncio.run(svc.backup()).ok
    remote = asyncio.run(svc.list_remote())[0]["path"].lstrip("/")
    src.sources = []

    bus = _CountingSourceBus(src)
    svc.bus = bus
    assert asyncio.run(svc.restore(remote, confirm=True)).ok
    stages = [(e.stage, n) for e, n in bus.stages
              if isinstance(e, BackupProgressEvent)]
    text = " | ".join(name for name, _ in stages)
    for step in ("下载", "校验备份包", "留一份", "正在恢复书源"):
        assert step in text, f"恢复少了这一拍：{step} → {text}"
    before = [n for name, n in stages if "正在恢复" not in name]
    assert before, "下载/校验/留底三拍一拍都没发"
    assert all(n == 0 for n in before), \
        f"进度拍发生时本地数据已经被改动了：{stages}"
    written = [n for name, n in stages if "正在恢复书源" in name]
    assert written and written[-1] == 2, f"进度报完时数据还没落库：{stages}"


class _CountingSourceBus(_SilentBus):
    """记账时顺手抄一份**那一拍当时**本地有几条书源（顺序 + 副作用时刻一起核）。

    不能在收尾后回看：那样每条记到的都是最终值，"进度是不是走在落库之前"就永远测不出来。
    """

    def __init__(self, src) -> None:
        super().__init__()
        self.src = src
        self.stages: list = []      # [(事件, 那一拍当时本地有几条书源)]

    def publish(self, event):
        self.events.append(event)
        self.stages.append((event, len(self.src.sources)))
        return []


def test_backup_and_restore_each_publish_one_finished_event(env):
    """收尾事件在 fixture 里记了账却从没人核过账——成/败各一条，且只有一条。"""
    src = FakeSource(sources=[{"bookSourceUrl": "u1"}], bookshelf=[])
    svc = _svc(env, src)
    assert asyncio.run(svc.backup()).ok
    _drain(env.bus)
    assert [(e.kind, e.ok) for e in env.captured] == [("all", True)], env.captured
    assert "备份成功" in env.captured[0].detail

    failed = asyncio.run(svc.restore("legado/backup/压根没有.zip", confirm=True))
    assert not failed.ok
    _drain(env.bus)
    assert [(e.kind, e.ok) for e in env.captured] == [("all", True),
                                                      ("restore", False)]


# --------------------------------------------------------------------------- #
# 6. txt 导出（只读缓存，不联网）
# --------------------------------------------------------------------------- #
def test_txt_export(env):
    cache = _make_export_book_cache(env.root / "cache")
    src = FakeSource(bookshelf=[
        {"url": "book1", "source_url": "src1", "name": "测试书", "author": "张三"}])
    svc = BookExportService(source_service=src, storage=env.storage, cache=cache)
    out = asyncio.run(svc.export_txt("book1"))
    assert out.exists()
    text = out.read_text(encoding="utf-8")
    assert "测试书" in text
    assert "第一章正文 你好世界" in text
    assert "第二章正文 第二段落" in text
    assert "第二卷" not in text  # 卷标题无正文


# --------------------------------------------------------------------------- #
# 7. epub 导出产物可被 ebooklib 读回
# --------------------------------------------------------------------------- #
def test_epub_export_readback(env):
    from ebooklib import epub
    cache = _make_export_book_cache(env.root / "cache")
    src = FakeSource(bookshelf=[
        {"url": "book1", "source_url": "src1", "name": "EPUB测试书", "author": "李四"}])
    svc = BookExportService(source_service=src, storage=env.storage, cache=cache)
    out = asyncio.run(svc.export_epub("book1"))
    assert out.exists() and out.suffix == ".epub"

    book = epub.read_epub(out)
    assert book.get_metadata("DC", "title")[0][0] == "EPUB测试书"
    # 应有 2 个正文 xhtml（卷标题不计；EpubNav 是导航页，按文件名区分）
    htmls = [i for i in book.get_items()
             if i.get_type() == 9 and i.file_name.startswith("chap_")]
    assert len(htmls) == 2


# --------------------------------------------------------------------------- #
# 7b. 导出进度事件（整本导出的分母是"已缓存章数"，不是目录条数）
# --------------------------------------------------------------------------- #
class _BrokenExportBus(_SilentBus):
    """**进度**事件一发就炸的总线：进度不能把导出一起带走。"""

    def publish(self, event):
        if isinstance(event, ExportProgressEvent):
            raise RuntimeError("事件环已经关了")
        return super().publish(event)


def _export_service(env, cache, bus=None, name="导出测试书") -> BookExportService:
    src = FakeSource(bookshelf=[
        {"url": "book1", "source_url": "src1", "name": name, "author": "张三"}])
    svc = BookExportService(source_service=src, storage=env.storage, cache=cache)
    svc.bus = bus
    return svc


def _export_progress(bus) -> list:
    return [e for e in bus.events if isinstance(e, ExportProgressEvent)]


def test_txt_export_publishes_progress_beats(env):
    """导出几十秒，返回值只在最后才有：中途必须靠事件说话。"""
    cache = _make_export_book_cache(env.root / "cache")
    bus = _SilentBus()
    out = asyncio.run(_export_service(env, cache, bus).export_txt("book1"))

    stages = _export_progress(bus)
    assert stages, "txt 导出一个进度事件都没发"
    assert {e.kind for e in stages} == {"txt"}
    text = " | ".join(e.stage for e in stages)
    # 合并窗口内只有第一章与"末拍"漏得出来——"正在写入第 2 章"就是那条末拍，
    # 没有它，进度条会永远停在 1/2（导出已经写完了两章）。
    assert "正在写入第 1 章" in text, text
    assert "正在写入第 2 章" in text, text
    assert "写文件" in text, text          # 末拍：真正落盘前那一拍
    assert stages[-1].done == 2            # 两章都写完
    assert out.exists()                    # 该发生的仍然发生


def test_epub_export_progress_denominator_is_cached_only(env):
    """分母必须是**已缓存**章数：拿目录条数当分母，进度条永远走不满。

    这份缓存目录 6 章（+1 个卷标题），正文只缓存了 4 章——导出写的就是 4 章。
    """
    cache = _make_multi_chapter_cache(env.root / "cache", 6, cached=4)
    bus = _SilentBus()
    out = asyncio.run(_export_service(env, cache, bus).export_epub("book1"))

    stages = _export_progress(bus)
    assert stages and {e.kind for e in stages} == {"epub"}
    assert stages[0].stage == "正在准备书籍信息" and stages[0].done == 0
    assert all(e.total == 4 for e in stages), [(e.stage, e.total) for e in stages]
    assert stages[-1].stage == "正在打包 epub" and stages[-1].done == 4
    assert out.suffix == ".epub" and out.exists()


def test_export_progress_denominator_survives_a_broken_counts_reader(env):
    """读不到计数时进度照发，只是分母留 0（界面走不定长条），不能因此不发。"""
    real = _make_export_book_cache(env.root / "cache")

    class _Counts:
        def __init__(self, inner):
            self.inner = inner

        def load_catalog(self, s, b):
            return self.inner.load_catalog(s, b)

        def load_content(self, s, b, c):
            return self.inner.load_content(s, b, c)

        def cache_counts(self, s, b):
            raise OSError("磁盘读不到")

    bus = _SilentBus()
    out = asyncio.run(_export_service(env, _Counts(real), bus).export_txt("book1"))
    stages = _export_progress(bus)
    assert stages and all(e.total == 0 for e in stages), stages
    assert out.exists()


def test_export_progress_publishing_never_breaks_the_export(env):
    cache = _make_export_book_cache(env.root / "cache")
    out = asyncio.run(_export_service(env, cache, _BrokenExportBus()).export_txt("book1"))
    assert out.exists()          # 进度是装饰，导出才是正事


def test_export_without_a_bus_still_completes(env, caplog):
    """没总线（无头跑批 / 服务表还没接好）：导出照做，而且**别拿异常兜 this**。

    不认 ``self.bus is None`` 的话，每一章都走一遍 ``None.publish`` 再被 except 吃掉，
    导出结果看不出差别，日志却刷满 WARNING——所以这里两样都钉。
    """
    import logging
    cache = _make_export_book_cache(env.root / "cache")
    with caplog.at_level(logging.WARNING):
        out = asyncio.run(_export_service(env, cache, None).export_txt("book1"))
    assert out.exists()
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING], \
        "没有总线时每章都在刷'进度发布失败'"


def test_export_progress_reaches_handlers_while_the_export_runs(env):
    """导出通体是同步磁盘活，**不让出循环的话进度就只是攒着**。

    ``EventBus.publish`` 只把处理器排进当前循环（设计 §5.11）。这里用一个
    "publish 就 create_task" 的总线 + 每章睡 50ms 的慢缓存，核一件事：
    读到最后一章时，前面那一拍的处理器**已经跑过**了。
    变异核验：拿掉 ``_paced`` 里的 ``await asyncio.sleep(0)`` → 本用例红
    （八章全读完了处理器一次都没跑，界面就是"进度条不动，然后一下子完成"）。
    """
    real = _make_multi_chapter_cache(env.root / "cache", 6)
    chapters = real.load_catalog("src1", "book1")

    class _SlowCache:
        def __init__(self, inner, last_url):
            self.inner = inner
            self.last_url = last_url
            self.ran_before_last = -1

        def load_catalog(self, s, b):
            return self.inner.load_catalog(s, b)

        def cache_counts(self, s, b):
            return self.inner.cache_counts(s, b)

        def load_content(self, s, b, c):
            # 读到最后一章时回头看：之前的进度拍送达了几次（-1 = 从未被记录）
            if c == self.last_url:
                self.ran_before_last = bus.ran
            time.sleep(0.05)
            return self.inner.load_content(s, b, c)

    class _DispatchingBus:
        """与真 EventBus 同构：publish 当场返回，处理器稍后在**同一条循环**上跑。"""

        def __init__(self):
            self.ran = 0
            self.stages: list = []

        def publish(self, event):
            if isinstance(event, ExportProgressEvent):
                asyncio.get_running_loop().create_task(self._deliver(event))
            return []

        async def _deliver(self, event):
            self.ran += 1              # 处理器开跑（真链路下一步才跳去界面循环）
            await asyncio.sleep(0)
            self.stages.append((event.stage, event.done, event.total))

    bus = _DispatchingBus()
    slow = _SlowCache(real, chapters[5].url)   # 目录末条是卷标题，没有正文
    out = asyncio.run(_export_service(env, slow, bus).export_txt("book1"))

    assert len(chapters) >= 7, "目录太短，测不出'中途'这件事"
    assert slow.ran_before_last >= 1, (
        f"读到最后一章时进度处理器一次都没跑过（导出把循环占死了）")
    assert bus.ran > slow.ran_before_last, "末拍没发：条会停在半路"
    assert [s[1] for s in bus.stages] == sorted(s[1] for s in bus.stages)
    assert out.exists()                # 该发生的仍然发生


# --------------------------------------------------------------------------- #
# 8. 更新检查 stub：发现新版本 → UpdateInfo + 事件
# --------------------------------------------------------------------------- #
def test_check_update_new_version(env):
    async def fake_get_json(url):
        return {
            "tag_name": "v1.2.0",
            "name": "Release 1.2.0",
            "body": "修复若干问题",
            "assets": [{"name": "legado-1.2.0.exe",
                        "browser_download_url": "https://x/legado-1.2.0.exe",
                        "size": 123}],
        }
    settings = FakeSettings()
    captured: list = []

    async def _on_update(ev):
        captured.append(ev)

    env.bus.subscribe(UpdateAvailableEvent, _on_update)

    svc = UpdateService(storage=env.storage, settings=settings,
                        http_get_json=fake_get_json, current_version="1.1.0")
    svc.bus = env.bus
    info = asyncio.run(svc.check_update())
    assert isinstance(info, UpdateInfo)
    assert info.version == "1.2.0"
    assert info.url.endswith(".exe")
    assert settings.get("update.last_check_time")  # 已记录检查时间
    _drain(env.bus)
    assert len(captured) == 1
    assert captured[0].version == "1.2.0"


# --------------------------------------------------------------------------- #
# 9. 当前版本已是最新 → None，不发事件
# --------------------------------------------------------------------------- #
def test_check_update_no_update(env):
    async def fake_get_json(url):
        return {"tag_name": "v1.0.0", "body": "old", "assets": []}
    svc = UpdateService(storage=env.storage, settings=FakeSettings(),
                        http_get_json=fake_get_json, current_version="1.2.0")
    svc.bus = env.bus
    info = asyncio.run(svc.check_update())
    assert info is None


# --------------------------------------------------------------------------- #
# 10. 已忽略版本 → None
# --------------------------------------------------------------------------- #
def test_ignored_version(env):
    async def fake_get_json(url):
        return {"tag_name": "v2.0.0", "body": "new",
                "assets": [{"name": "a.exe", "browser_download_url": "https://x/a.exe"}]}
    settings = FakeSettings()
    svc = UpdateService(storage=env.storage, settings=settings,
                        http_get_json=fake_get_json, current_version="1.0.0")
    svc.bus = env.bus
    asyncio.run(svc.ignore_version("2.0.0"))
    assert settings.get("update.ignored_version") == "2.0.0"
    info = asyncio.run(svc.check_update())
    assert info is None


# --------------------------------------------------------------------------- #
# 11. 版本解析 / 比较纯函数
# --------------------------------------------------------------------------- #
def test_version_parse_and_compare():
    assert _version_tuple("v1.2.3") == (1, 2, 3)
    assert _version_tuple("1.10") > _version_tuple("1.9")
    info = _parse_release({
        "tag_name": "v3.0.0", "body": "notes",
        "assets": [{"name": "legado-3.0.0.exe",
                    "browser_download_url": "https://x/3.exe", "size": 1}]})
    assert info.version == "3.0.0"
    assert info.url == "https://x/3.exe"


# --------------------------------------------------------------------------- #
# 12. 下载校验：digest 不符抛错且不落盘
# --------------------------------------------------------------------------- #
def test_update_download_hash_mismatch(env):
    async def fake_download(url):
        return b"corrupt-bytes"
    svc = UpdateService(storage=env.storage, settings=FakeSettings(),
                        http_download=fake_download, current_version="1.0.0")
    info = UpdateInfo(version="9.9.9", notes="", url="https://x/a.exe",
                      digest="0" * 64)  # 期望全 0，实际必然不符
    with pytest.raises(RuntimeError, match="哈希"):
        asyncio.run(svc.download(info))
