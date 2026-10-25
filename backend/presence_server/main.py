"""Ứng dụng FastAPI: API + dashboard tĩnh + cầu nối MQTT + bộ chạy tác vụ huấn luyện.

    uvicorn presence_server.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from .api import router
from .config import Settings, load_settings
from .db import Database
from .jobs import JobRunner
from .service import PresenceService


def create_app(settings: Settings | None = None, start_mqtt: bool | None = None) -> FastAPI:
    settings = settings or load_settings()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db = Database(settings.db_path)
        bridge = None
        use_mqtt = settings.mqtt_enabled if start_mqtt is None else start_mqtt
        if use_mqtt:
            from .mqtt_bridge import MqttBridge

            bridge = MqttBridge(settings)
        service = PresenceService(settings, db, bridge)
        if bridge:
            bridge.svc = service
            bridge.start()
        app.state.settings = settings
        app.state.db = db
        app.state.service = service
        app.state.jobs = JobRunner(settings, db, service)
        app.state.bridge = bridge
        if not settings.admin_token:
            logging.getLogger("presence").warning("ADMIN_TOKEN chưa đặt: API quản trị không có xác thực (chỉ dùng trong LAN)")
        if not settings.signing_key:
            logging.getLogger("presence").warning(
                "MODEL_SIGNING_KEY chưa đặt: gói mô hình sẽ KHÔNG được ký; firmware mặc định "
                "(CONFIG_PRESENCE_MODEL_REQUIRE_SIGNATURE=y) từ chối gói chưa ký. Tạo khóa: python tools/keys.py init")
        try:
            yield
        finally:
            if bridge:
                bridge.stop()
            for w in service.writers.values():
                w.close()
            db.close()

    app = FastAPI(title="Presence S3 AI", version="0.1.0", lifespan=lifespan)
    app.include_router(router)
    if settings.dashboard_dir.is_dir():
        app.mount("/dashboard", StaticFiles(directory=str(settings.dashboard_dir), html=True), name="dashboard")

        @app.get("/", include_in_schema=False)
        def root():
            return RedirectResponse("/dashboard/")

    return app


def __getattr__(name: str):
    # `uvicorn presence_server.main:app` tạo ứng dụng khi cần (import module không tự đọc cấu hình)
    if name == "app":
        globals()["app"] = create_app()
        return globals()["app"]
    raise AttributeError(name)
