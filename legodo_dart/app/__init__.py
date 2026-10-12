"""本机版本号：整个应用只有这一处定义。

PyInstaller 打出来的 exe 不带 git 信息，版本号必须由代码给；更新检查拿它当比较基准
（见 :class:`app.services.tools.update_service.UpdateService`）。
"""

APP_VERSION = "1.0.0"
