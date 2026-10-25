"""Cấu hình từ biến môi trường (hoặc file .env, xem deployment/.env.example). Không có bí mật trong mã nguồn."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def load_dotenv(path: Path) -> None:
    """Đọc KEY=VALUE từ .env vào os.environ (không ghi đè biến đã có)."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        os.environ.setdefault(k, v)


def _bool(v: str | None, default: bool = False) -> bool:
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    data_dir: Path
    mqtt_host: str = "localhost"
    mqtt_port: int = 1883
    mqtt_username: str = ""
    mqtt_password: str = ""
    mqtt_tls: bool = False
    mqtt_ca_file: str = ""
    mqtt_enabled: bool = True
    # Mã phiên MQTT bền của backend: broker giữ mẫu cho mã này khi backend vắng mặt. Mỗi mã chỉ một backend chạy.
    mqtt_client_id: str = "presence-backend"
    topic_prefix: str = "presence/v1"
    device_tokens: list[str] = field(default_factory=list)
    admin_token: str = ""
    signing_key: str = ""
    training_python: str = sys.executable
    training_config: str = ""
    live_minutes: int = 15
    event_snapshot_minutes: int = 10
    dashboard_dir: Path = REPO_ROOT / "dashboard"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "presence.db"

    @property
    def sessions_dir(self) -> Path:
        return self.data_dir / "sessions"

    @property
    def models_dir(self) -> Path:
        return self.data_dir / "models"

    @property
    def jobs_dir(self) -> Path:
        return self.data_dir / "jobs"


def load_settings(env_file: str | None = None) -> Settings:
    load_dotenv(Path(env_file) if env_file else Path(os.environ.get("PRESENCE_ENV_FILE", REPO_ROOT / ".env")))
    e = os.environ.get
    s = Settings(
        data_dir=Path(e("PRESENCE_DATA_DIR", str(REPO_ROOT / "data"))).resolve(),
        mqtt_host=e("MQTT_HOST", "localhost"),
        mqtt_port=int(e("MQTT_PORT", "1883")),
        mqtt_username=e("MQTT_USERNAME", ""),
        mqtt_password=e("MQTT_PASSWORD", ""),
        mqtt_tls=_bool(e("MQTT_TLS")),
        mqtt_ca_file=e("MQTT_CA_FILE", ""),
        mqtt_enabled=_bool(e("MQTT_ENABLED"), True),
        mqtt_client_id=e("MQTT_CLIENT_ID", "presence-backend"),
        topic_prefix=e("MQTT_TOPIC_PREFIX", "presence/v1").rstrip("/"),
        device_tokens=[t.strip() for t in e("DEVICE_TOKENS", "").split(",") if t.strip()],
        admin_token=e("ADMIN_TOKEN", ""),
        signing_key=e("MODEL_SIGNING_KEY", ""),
        training_python=e("TRAINING_PYTHON", sys.executable),
        training_config=e("TRAINING_CONFIG", ""),
        live_minutes=int(e("LIVE_MINUTES", "15")),
        event_snapshot_minutes=int(e("EVENT_SNAPSHOT_MINUTES", "10")),
    )
    if e("DASHBOARD_DIR"):
        s.dashboard_dir = Path(e("DASHBOARD_DIR", "")).resolve()
    for d in (s.data_dir, s.sessions_dir, s.models_dir, s.jobs_dir):
        d.mkdir(parents=True, exist_ok=True)
    return s
