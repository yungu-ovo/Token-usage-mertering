"""登录自启用的看板启动器（P1）。

为什么需要这个文件而不是在注册表里直接写 uvicorn 命令：
``uvicorn app.main:app`` 依赖**当前工作目录是项目根**，而注册表 Run 键的
命令行没有「设置 cwd」的能力，只能借助 ``cmd /c "cd /d ... && ..."``——
那会多引入一个 shell 进程和一层引号转义。这个启动器把 chdir 收进 Python，
Run 键里只剩一条干净的命令行：``pythonw.exe <此文件>``。

用 ``pythonw.exe`` 启动（无控制台窗口）；找不到时退回 ``python.exe``。
stdout/stderr 重定向到 ``data/autostart.log``——自启进程看不见控制台，
没日志就等于没告警（uvicorn 访问日志、我们的 app 日志、 traceback 全都落这里）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _redirect_output_to_log() -> Path:
    """把进程级的 stdout/stderr 接到日志文件（自启进程没有控制台可看）。"""
    log_path = PROJECT_ROOT / "data" / "autostart.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = os.open(str(log_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    os.dup2(handle, 1)  # stdout
    os.dup2(handle, 2)  # stderr
    os.close(handle)
    return log_path


def main() -> None:
    # ``python scripts/xxx.py`` 的 sys.path[0] 是脚本所在目录，不是 cwd——
    # 不把项目根插进去，import app 会直接 ModuleNotFoundError（实测踩过）。
    sys.path.insert(0, str(PROJECT_ROOT))
    os.chdir(PROJECT_ROOT)  # uvicorn 的 app.main 字符串导入同样依赖 cwd
    log_path = _redirect_output_to_log()

    import uvicorn

    from app import __version__, config

    print(
        f"[autostart] {config.APP_NAME} v{__version__} pid={os.getpid()} "
        f"启动于 http://{config.HOST}:{config.PORT}（日志：{log_path}）",
        flush=True,
    )
    uvicorn.run(
        "app.main:app",
        host=config.HOST,
        port=config.PORT,
        reload=False,
    )


if __name__ == "__main__":
    main()
