from __future__ import annotations

import argparse
import os

import uvicorn


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="启动 Ostensory 服务端")
    parser.add_argument("--host", default=os.environ.get("OSTENSORY_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("OSTENSORY_PORT", "8000")))
    parser.add_argument(
        "--reload",
        action="store_true",
        default=os.environ.get("OSTENSORY_RELOAD", "0") == "1",
        help="开发模式热重载",
    )
    args = parser.parse_args(argv)
    print(f"Ostensory: http://{args.host}:{args.port}/  （后台默认 /admin）")
    uvicorn.run(
        "app.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        workers=None if args.reload else 1,
        proxy_headers=False,
        forwarded_allow_ips="127.0.0.1",
    )


if __name__ == "__main__":
    main()