"""支持通过 ``python -m app collect`` 单次采集。"""

from app.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
