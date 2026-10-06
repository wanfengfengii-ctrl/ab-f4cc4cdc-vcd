"""python -m vcd_service 入口。

环境变量：VCD_HOST（默认 0.0.0.0）、VCD_PORT（默认 8080）。
"""

import logging
import os

from .server import build_server


def main():
    logging.basicConfig(
        level=os.environ.get("VCD_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    host = os.environ.get("VCD_HOST", "0.0.0.0")
    port = int(os.environ.get("VCD_PORT", "8080"))
    server = build_server(host, port)
    logging.getLogger("vcd_service").info(
        "VCD 窗口服务监听 http://%s:%d", host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
