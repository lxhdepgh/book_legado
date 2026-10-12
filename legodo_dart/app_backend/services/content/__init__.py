"""内容形态域服务包（M4，设计 §4.2 内容形态域服务表）。

本子域承载"非书源网络抓取"的内容形态：本地书导入（LocalBookService）、
RSS（RssService，由并行子线交付）、朗读/媒体播放（ReadAloud/MediaPlay）。
本包内所有服务**零 flet import**，视图层经 ServiceHub 取代理句柄调用。
"""
