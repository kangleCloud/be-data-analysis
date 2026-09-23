"""可选的轻量健康接口。"""

from fastapi import FastAPI

from app.core import msg


def create_app() -> FastAPI:
    application = FastAPI(title="be-data-analysis", version="1.0.0")

    @application.get("/health", tags=["系统接口"], summary="服务健康检查")
    def health():
        return msg.ok({"status": "ok"}, message="服务正常")

    return application


app = create_app()
