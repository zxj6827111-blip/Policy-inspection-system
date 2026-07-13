"""供 Windows pythonw 后台启动使用，避免无控制台环境中的日志句柄错误。"""

import sys
from pathlib import Path

import uvicorn


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# pythonw 没有控制台句柄；显式接管输出，避免底层库写入空句柄导致进程退出。
log_dir = Path(__file__).resolve().parent.parent / "data"
log_dir.mkdir(parents=True, exist_ok=True)
log_file = open(log_dir / "server.log", "a", encoding="utf-8", buffering=1)
sys.stdout = log_file
sys.stderr = log_file


if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host="127.0.0.1",
        port=8765,
        log_config=None,
        access_log=False,
    )
