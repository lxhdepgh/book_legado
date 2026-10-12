"""Windows 系统托盘：一条后台线程 + 一只消息窗口，点图标回调到注入的函数。

只做"能不能挂上、回调收不收得到"这一件事，不带 flet（服务层红线：零 flet import），
页面/窗口由调用方经回调自己碰。依赖 **pywin32**（Windows 专属，懒 import：非 Windows
或缺库时 :func:`is_available` 返回 False，调用方降级，绝不带着 ImportError 往上冒）。

为什么图标只在藏窗期间存在：老板键要的是"藏起来还得回得来"，不是常驻通知区的一个
小图标。恢复那一刻 :meth:`WinTray.stop` 删图标、关窗口线程，进程里不留后台线程。

回调走 Win32 消息，**跑在这条托盘线程上**，不在界面循环上——所以 ``on_activate``
里绝不能直接碰控件（项目硬约束：控件只认界面循环，见 :mod:`app.controllers.base`），
要碰得由调用方自己编组回去。

消息环的停止方式（探针实测过的这条）：从别的线程 ``PostMessage(WM_CLOSE)`` →
``wnd_proc`` 里 DestroyWindow → WM_DESTROY → PostQuitMessage → 阻塞在
``PumpMessages()`` 的线程返回。直接 join 一条正在 Pump 的线程是关不掉的。
"""
from __future__ import annotations

import io
import logging
import os
import tempfile
import threading

logger = logging.getLogger(__name__)

#: 回调消息号（WM_APP 起是应用私有区间，不与系统消息撞号）。
_CALLBACK_MSG = 0x8000 + 1
#: 鼠标消息：Shell_NotifyIcon 把它们塞在回调消息的 lParam 低 16 位里。
#: 当场对过 win32con（0x0201/0x0202/0x0203 = 左键按下/抬起/双击，0x0205 = **右键抬起**，
#: 0x0204 才是右键按下、0x0208 是中键抬起）。一次单击就会送来**两**拍（按下+抬起）、
#: 双击五拍，所以回调必须幂等——这件事由 :meth:`BossKeyService.restore` 自己挡
#: （藏窗标志先落，后到的那几拍直接 return）。
#: **不含 0x0200（鼠标移动）**——悬停就触发，留着等于"鼠标扫过通知区就把窗口弹回来"；
#: 第七十三轮桌面探针的对照臂量过：注入 0x0200 时窗口确实没回来，这一条挡对了。
_ACTIVATE_MESSAGES = frozenset({0x0201, 0x0202, 0x0203, 0x0205})

#: 起线程后等 ADD 结果的秒数：探针实测合法结构 0.01 s 内返回，超了就是卡住/没起来。
_START_TIMEOUT = 3.0
_JOIN_TIMEOUT = 2.0
#: szTip 在 1.0 结构里的上限（4.0 是 255，但这里用的是六元组那一版）。
_TIP_LIMIT = 63


def _load_backend():
    """懒加载 pywin32；返回 ``(win32api, win32con, win32gui)`` 或 None。"""
    try:
        import win32api
        import win32con
        import win32gui
    except Exception:                      # 非 Windows / 未装 pywin32
        return None
    if not (hasattr(win32gui, "Shell_NotifyIcon") and hasattr(win32gui, "NIM_ADD")):
        return None                        # 装了但没托盘入口（非 Windows 轮子）
    return win32api, win32con, win32gui


def is_available() -> bool:
    """这台机器上到底有没有托盘能力（Windows + pywin32）。"""
    return _load_backend() is not None


def _hicon_from_bytes(payload: bytes, win32con, win32gui):
    """PNG/JPEG 字节 → ``(HICON, 临时 .ico 路径)``。

    Win32 只认 ICONIMAGE，不认 PNG 流，所以必须先落一份 .ico。路径要交给调用方
    留着：进程活着的时候 HICON 的数据源不能删。
    """
    from PIL import Image

    img = Image.open(io.BytesIO(payload)).convert("RGBA").resize((32, 32))
    fd, path = tempfile.mkstemp(suffix=".ico", prefix="legado_tray_")
    os.close(fd)
    try:
        img.save(path, format="ICO", sizes=[(32, 32)])
        hicon = win32gui.LoadImage(0, path, win32con.IMAGE_ICON, 32, 32,
                                   win32con.LR_LOADFROMFILE)
        return hicon, path
    except Exception:
        os.remove(path)
        raise


class WinTray:
    """一次性的托盘图标：:meth:`start` 挂上，:meth:`stop` 摘掉并收线程。

    ``icon_bytes`` 缺省用系统应用图标（``IDI_APPLICATION``）——本项目没有 .ico
    资产，而这里要的只是一个"点得回来"的小标记，不是品牌露出。
    """

    def __init__(self, tooltip: str, on_activate,
                 icon_bytes: bytes | None = None,
                 class_name: str = "LegodoBossKeyTray") -> None:
        self._tooltip = str(tooltip or "")[:_TIP_LIMIT]
        self._on_activate = on_activate
        self._icon_bytes = icon_bytes
        self._class_name = class_name
        self._thread: threading.Thread | None = None
        self._hwnd: int | None = None
        self._ico_path: str | None = None
        self._ready = threading.Event()
        self._ok = False
        self._error = ""

    # ------------------------------------------------------------------ #
    @staticmethod
    def available() -> bool:
        return is_available()

    @property
    def ok(self) -> bool:
        """图标是否真的挂上了（``start`` 之后才有意义）。"""
        return self._ok

    @property
    def error(self) -> str:
        return self._error

    # ------------------------------------------------------------------ #
    def start(self) -> bool:
        """起线程挂图标；成功返回 True，任何失败把原因记在 :attr:`error` 上。"""
        if self._thread is not None:
            return self._ok
        thread = threading.Thread(target=self._run, name="bosskey-tray", daemon=True)
        self._thread = thread
        thread.start()
        if not self._ready.wait(_START_TIMEOUT):
            self._error = f"{_START_TIMEOUT}s 内托盘线程没给出结果"
            return False
        return self._ok

    def stop(self) -> None:
        """删图标、关消息窗口、等线程退出（幂等；没起来过也安全）。"""
        thread = self._thread
        hwnd = self._hwnd
        self._thread = None
        if thread is None:
            self._cleanup_icon_file()
            return
        if hwnd:
            try:
                backend = _load_backend()
                if backend is not None:
                    backend[2].PostMessage(hwnd, backend[1].WM_CLOSE, 0, 0)
            except Exception:              # 窗口已经没了：线程自己会退出
                logger.debug("托盘窗口关闭消息投递失败", exc_info=True)
        thread.join(_JOIN_TIMEOUT)
        if thread.is_alive():
            # daemon 线程不会拖住进程退出，这里只把事实记下来，不假装收干净了。
            logger.warning("托盘线程 %ss 内没退出", _JOIN_TIMEOUT)

    # ------------------------------------------------------------------ #
    def _run(self) -> None:
        backend = _load_backend()
        if backend is None:
            self._fail("这台机器没有托盘能力（非 Windows 或未装 pywin32）")
            return
        _, win32con, win32gui = backend
        self._con, self._gui = win32con, win32gui
        hwnd = 0
        try:
            hicon = self._load_icon()
            wc = win32gui.WNDCLASS()
            wc.lpfnWndProc = self._wnd_proc
            wc.lpszClassName = self._class_name
            wc.hInstance = win32gui.GetModuleHandle(None)
            wc.hIcon = hicon
            wc.hCursor = win32gui.LoadCursor(0, win32con.IDC_ARROW)
            try:
                win32gui.RegisterClass(wc)
            except Exception:
                # 重复 start（上一只图标还在）：类名已注册，复用现成的类。
                pass
            hwnd = win32gui.CreateWindowEx(
                0, self._class_name, "legodo-bosskey-tray", 0, 0, 0, 0, 0,
                getattr(win32gui, "HWND_MESSAGE", None), None, wc.hInstance, None)
            if not hwnd:
                raise OSError("CreateWindowEx 返回 0（消息窗口没建起来）")
            self._hwnd = hwnd
            flags = (win32gui.NIF_MESSAGE | win32gui.NIF_ICON | win32gui.NIF_TIP)
            # 六元组是唯一实测可用的写法：十元组把尾部的 int 当字符串要（TypeError），
            # NIM_SETVERSION 在 pywin32 里签名不对（同样是 TypeError）。
            win32gui.Shell_NotifyIcon(
                win32gui.NIM_ADD,
                (hwnd, 1, flags, _CALLBACK_MSG, hicon, self._tooltip))
        except Exception as exc:
            self._fail(f"{type(exc).__name__}: {exc}")
            if hwnd:
                try:
                    win32gui.DestroyWindow(hwnd)
                except Exception:
                    pass
                self._hwnd = None
            return
        self._ok = True
        self._ready.set()
        try:
            win32gui.PumpMessages()        # 直到 WM_CLOSE → PostQuitMessage
        except Exception:
            logger.debug("托盘消息环异常退出", exc_info=True)
        finally:
            self._delete_icon()
            self._ok = False
            self._cleanup_icon_file()

    def _fail(self, reason: str) -> None:
        self._ok = False
        self._error = reason
        logger.warning("托盘图标挂载失败：%s", reason)
        self._cleanup_icon_file()
        self._ready.set()

    def _load_icon(self):
        win32con, win32gui = self._con, self._gui
        if self._icon_bytes:
            try:
                hicon, path = _hicon_from_bytes(self._icon_bytes, win32con, win32gui)
                self._ico_path = path
                return hicon
            except Exception:
                logger.warning("托盘图标解码失败，退回系统应用图标", exc_info=True)
        try:
            return win32gui.LoadIcon(0, win32con.IDI_APPLICATION)
        except Exception:
            return 0                       # 连系统图标都取不到：空白图标，仍可点

    def _delete_icon(self) -> None:
        win32gui = getattr(self, "_gui", None)
        if win32gui is None or not self._hwnd:
            return
        try:
            flags = win32gui.NIF_MESSAGE | win32gui.NIF_ICON | win32gui.NIF_TIP
            win32gui.Shell_NotifyIcon(
                win32gui.NIM_DELETE, (self._hwnd, 1, flags, _CALLBACK_MSG, 0, ""))
        except Exception:
            logger.debug("托盘图标删除失败", exc_info=True)

    def _cleanup_icon_file(self) -> None:
        if self._ico_path:
            try:
                os.remove(self._ico_path)
            except OSError:
                pass
            self._ico_path = None

    def _wnd_proc(self, hwnd, msg, wparam, lparam) -> int:
        if msg == _CALLBACK_MSG:
            if (lparam & 0xFFFF) in _ACTIVATE_MESSAGES:
                self._activate()
            return 0
        if msg == self._con.WM_DESTROY:
            try:
                self._gui.PostQuitMessage(0)
                return 0
            except Exception:
                logger.debug("PostQuitMessage 失败", exc_info=True)
        return self._gui.DefWindowProc(hwnd, msg, wparam, lparam)

    def _activate(self) -> None:
        try:
            self._on_activate()
        except Exception:
            logger.exception("托盘回调失败")
