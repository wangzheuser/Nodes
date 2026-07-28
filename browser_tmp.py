"""浏览器临时目录的落盘位置。

DrissionPage 的 auto_port() 会把 `临时目录/DrissionPage/autoPortData/<端口>` 直接当作
浏览器的 user-data-dir，而它只在正常断连时清理——进程被强杀就永久残留。默认临时目录在
系统盘，长期跑批会把 C 盘写满（实测积累到 18GB+），进而拖垮其它依赖系统盘的程序。

作者: wangqiupei
"""

from __future__ import annotations

import os
import tempfile

# 目录名用项目名，便于在临时盘里一眼看出残留是谁产生的。
_TMP_DIR_NAME = "Nodes"

# Windows 首选的非系统盘。
_WINDOWS_PREFERRED_DRIVE = "D:\\"


def browser_tmp_root() -> str:
    """返回浏览器临时数据的根目录（不创建目录）。

    Windows 且 D 盘可用时用 D:\\Temp\\Nodes；否则退回系统临时目录，
    保证 Linux/容器环境同样可用。
    """
    if os.name == "nt" and os.path.isdir(_WINDOWS_PREFERRED_DRIVE):
        return os.path.join(_WINDOWS_PREFERRED_DRIVE, "Temp", _TMP_DIR_NAME)
    return os.path.join(tempfile.gettempdir(), _TMP_DIR_NAME)
