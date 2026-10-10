"""StateBar：底部状态行（对齐 Dart 的 state_bar.dart）。

四屏共用一份实现：书架、书源列表、阅读器、外观设置。动作回报优先占位，
没有回报时才回这屏自己的常态读数（"书架共 N 本"/"共 N 条 | 启用…"）。

关键约束：**加载失败不许报成"共 0 条"**（那是把"没拿到数据"说成"没有数据"）。

使用示例：
    # Controller 中
    view.set_status("已删除 3 条书源")  # 动作回报
    
    # 视图装配时
    self._status = StateBar(
        failed_text="加载失败",
        loading_text="正在加载…",
        data_text=lambda: f"书架共 {len(items)} 本",
    )
    
    # 错误状态
    self._status.show_error("网络超时")
"""
from __future__ import annotations

import flet as ft


class StateBar(ft.Container):
    """底部状态行：动作消息 > 失败 > 加载中 > 数据文案（四态优先级）。
    
    Parameters
    ----------
    failed_text : str
        失败时的文案（如"加载失败"）
    loading_text : str
        加载中的文案（如"正在加载…"）
    data_text : str | callable
        数据到位时的常态读数（可传函数动态计算）
    is_error : bool
        当前是否为错误状态（红字显示）
    visible : bool
        是否可见（默认 True）
    """
    
    def __init__(
        self,
        *,
        failed_text: str = "加载失败",
        loading_text: str = "正在加载…",
        data_text: str | None = None,
        is_error: bool = False,
        visible: bool = True,
    ) -> None:
        super().__init__()
        self._failed_text = failed_text
        self._loading_text = loading_text
        self._data_text = data_text  # 可以是字符串或 callable
        self._is_error = is_error
        self.visible = visible
        
        #: 当前动作回报（非空时优先显示）
        self._message: str = ""
        
        #: 状态文本控件
        self._text = ft.Text("", size=12, color=ft.Colors.GREY_600)
        
        self.content = ft.Padding(
            padding=ft.Padding(12, 6, 12, 6),
            child=self._text,
        )
        
        # 初始渲染
        self._update_text()
    
    # ------------------------------------------------------------------ #
    @property
    def message(self) -> str:
        return self._message
    
    @message.setter
    def message(self, value: str) -> None:
        """设置动作回报（清空时回退到常态读数）。"""
        self._message = value or ""
        self._update_text()
    
    @property
    def is_error(self) -> bool:
        return self._is_error
    
    @is_error.setter
    def is_error(self, value: bool) -> None:
        self._is_error = value
        self._text.color = ft.Colors.RED_700 if value else ft.Colors.GREY_600
        self._update_text()
    
    # ------------------------------------------------------------------ #
    def show_message(self, text: str) -> None:
        """显示动作回报（如"已删除 3 条书源"）。"""
        self.message = text
    
    def show_error(self, text: str) -> None:
        """显示错误状态（红字）。"""
        self._message = text
        self.is_error = True
    
    def show_loading(self) -> None:
        """切换到加载状态（清空动作回报）。"""
        self._message = ""
        self.is_error = False
        self._update_text()
    
    def show_data(self, data_text: str | None = None) -> None:
        """切换到数据就绪状态。
        
        Parameters
        ----------
        data_text : str, optional
            临时覆盖默认的 data_text（用于一次性更新）
        """
        self._message = ""
        self.is_error = False
        if data_text is not None:
            self._data_text = data_text
        self._update_text()
    
    def clear(self) -> None:
        """清空所有状态（隐藏状态行）。"""
        self._message = ""
        self.is_error = False
        self.visible = False
        self._text.value = ""
    
    # ------------------------------------------------------------------ #
    def _update_text(self) -> None:
        """按四态优先级更新文本：动作消息 > 失败 > 加载中 > 数据文案。"""
        if self._message:
            # 动作回报优先
            text = self._message
        elif self._is_error:
            # 失败状态
            text = self._failed_text
        elif self._data_text is None:
            # 加载中（data_text 为 None 表示还没拿到数据）
            text = self._loading_text
        else:
            # 数据就绪
            text = self._data_text() if callable(self._data_text) else self._data_text
        
        self._text.value = text or ""
