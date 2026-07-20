"""支持通过 ``python -m app`` 启动服务。"""

from app.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
