"""本机直跑入口：单进程单 worker。

运行：python -m web
  WEB_PORT  端口（默认 8000）
  WEB_HOST  监听地址（默认 127.0.0.1；本服务无认证，能访问即能触发
            真实云操作——放宽到 0.0.0.0 / 内网地址由运行者自担，
            spec 运行边界为「只监听 127.0.0.1 或内网」）
"""
import os

import uvicorn

from .app import create_app


def main():
    port = int(os.environ.get("WEB_PORT", "8000"))
    host = os.environ.get("WEB_HOST", "127.0.0.1")
    uvicorn.run(create_app(), host=host, port=port)


if __name__ == "__main__":
    main()
