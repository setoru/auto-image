"""本机直跑入口：单进程单 worker，只监听 127.0.0.1（无认证服务不得暴露公网）。

运行：python -m web        （端口可用 WEB_PORT 覆盖，默认 8000）
"""
import os

import uvicorn

from .app import create_app


def main():
    port = int(os.environ.get("WEB_PORT", "8000"))
    uvicorn.run(create_app(), host="127.0.0.1", port=port)


if __name__ == "__main__":
    main()
