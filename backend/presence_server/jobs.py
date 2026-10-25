"""Tác vụ huấn luyện nền: chạy ``python -m presence_ml.cli train`` trong tiến trình con, mỗi lúc một tác vụ.

Tiến trình con in ``PROGRESS {json}``; backend lưu dòng mới nhất vào jobs.progress_json và toàn bộ log vào
data/jobs/<id>/log.txt. Kết thúc thành công => đăng ký mô hình trạng thái "evaluated" (chưa được phát hành).
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from pathlib import Path

from .config import REPO_ROOT, Settings
from .db import Database, jdumps, jloads, now_ms
from .service import PresenceService, ServiceError

log = logging.getLogger("presence.jobs")


class JobRunner:
    def __init__(self, settings: Settings, db: Database, service: PresenceService):
        self.s = settings
        self.db = db
        self.svc = service
        self.lock = threading.Lock()
        self.proc: subprocess.Popen | None = None
        self.current: int | None = None
        # Tác vụ dang dở từ lần chạy trước của backend
        self.db.execute("UPDATE jobs SET status='failed', error='backend khởi động lại khi tác vụ đang chạy', "
                        "finished_at=? WHERE status IN ('queued','running')", (now_ms(),))

    def submit(self, request: dict) -> dict:
        with self.lock:
            if self.current is not None:
                raise ServiceError("đang có tác vụ huấn luyện chạy", 409)
            cur = self.db.execute("INSERT INTO jobs(created_at, status, request_json) VALUES (?,?,?)",
                                  (now_ms(), "queued", jdumps(request)))
            job_id = cur.lastrowid
            self.current = job_id
        t = threading.Thread(target=self._run, args=(job_id, request), daemon=True)
        t.start()
        return self.get(job_id)

    def get(self, job_id: int) -> dict:
        j = self.db.one("SELECT * FROM jobs WHERE job_id=?", (job_id,))
        if not j:
            raise ServiceError("không có tác vụ", 404)
        j["request"] = jloads(j.pop("request_json"), {})
        j["progress"] = jloads(j.pop("progress_json"), {})
        return j

    def list(self) -> list[dict]:
        return [self.get(r["job_id"]) for r in self.db.all("SELECT job_id FROM jobs ORDER BY job_id DESC LIMIT 50")]

    def log_tail(self, job_id: int, lines: int = 200) -> str:
        j = self.get(job_id)
        p = Path(j["log_path"]) if j.get("log_path") else None
        if not p or not p.exists():
            return ""
        return "\n".join(p.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])

    def cancel(self, job_id: int) -> dict:
        with self.lock:
            if self.current == job_id and self.proc and self.proc.poll() is None:
                self.proc.terminate()
        return self.get(job_id)

    def _run(self, job_id: int, request: dict) -> None:
        jdir = self.s.jobs_dir / str(job_id)
        jdir.mkdir(parents=True, exist_ok=True)
        log_path = jdir / "log.txt"
        try:
            include_sim = bool(request.get("include_simulated"))
            ds = self.svc.build_dataset(jdir / "dataset.json", include_simulated=include_sim)
            if not ds["sessions"]:
                raise ServiceError("chưa có phiên nào có nhãn để huấn luyện")
            row = self.db.one("SELECT MAX(model_version) v FROM models")
            version = max(int(row["v"] or 0) + 1, 1)
            model_id = time.strftime("m%Y%m%d-%H%M%S", time.gmtime())
            out_dir = self.s.models_dir / model_id
            cmd = [self.s.training_python, "-m", "presence_ml.cli", "train", "--dataset", str(jdir / "dataset.json"),
                   "--out", str(out_dir), "--model-version", str(version), "--model-id", model_id]
            cfg_path = request.get("config_path") or self.s.training_config
            overrides = request.get("overrides") or {}
            if overrides:
                import tomllib  # noqa: F401  (chỉ kiểm tra có tomllib)

                ov = jdir / "overrides.toml"
                ov.write_text(_to_toml(overrides), encoding="utf-8")
                if cfg_path:
                    # gộp: file người dùng trước, overrides sau
                    from presence_ml.config import load_config

                    merged = load_config(cfg_path, overrides)
                    ov.write_text(_to_toml(merged), encoding="utf-8")
                cfg_path = str(ov)
            if cfg_path:
                cmd += ["--config", cfg_path]
            if self.s.signing_key:
                cmd += ["--key", self.s.signing_key]
            if include_sim:
                cmd += ["--allow-simulated"]
            self.db.execute("UPDATE jobs SET status='running', started_at=?, log_path=?, dataset_path=?, model_id=? "
                            "WHERE job_id=?", (now_ms(), str(log_path), str(jdir / "dataset.json"), model_id, job_id))
            env = dict(os.environ)
            env["PYTHONPATH"] = os.pathsep.join([str(REPO_ROOT / "training"), env.get("PYTHONPATH", "")])
            env.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")
            with open(log_path, "w", encoding="utf-8") as lf:
                lf.write("$ " + " ".join(cmd) + "\n")
                lf.flush()
                self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                             encoding="utf-8", errors="replace", env=env, cwd=str(REPO_ROOT))
                last_error = None
                for line in self.proc.stdout:  # type: ignore[union-attr]
                    lf.write(line)
                    lf.flush()
                    if line.startswith("PROGRESS "):
                        try:
                            pg = json.loads(line[9:])
                        except ValueError:
                            continue
                        if pg.get("stage") == "error":
                            last_error = pg.get("error")
                        self.db.execute("UPDATE jobs SET progress_json=? WHERE job_id=?", (jdumps(pg), job_id))
                    elif line.startswith("LỖI:"):
                        last_error = line[4:].strip()
                rc = self.proc.wait()
            if rc != 0:
                raise ServiceError(last_error or f"tiến trình huấn luyện kết thúc với mã {rc} (xem log)")
            self._register_model(job_id, model_id, version, out_dir)
            self.db.execute("UPDATE jobs SET status='done', finished_at=? WHERE job_id=?", (now_ms(), job_id))
        except Exception as e:  # noqa: BLE001
            log.exception("job %s", job_id)
            self.db.execute("UPDATE jobs SET status='failed', finished_at=?, error=? WHERE job_id=?",
                            (now_ms(), str(e), job_id))
        finally:
            with self.lock:
                self.current = None
                self.proc = None

    def _register_model(self, job_id: int, model_id: str, version: int, out_dir: Path) -> None:
        report = json.loads((out_dir / "report.json").read_text(encoding="utf-8"))
        pkg = report["package"]
        self.db.execute(
            "INSERT INTO models(model_id, model_version, created_at, job_id, status, dir, package_sha256, package_size, "
            "signed, simulated, conclusive, summary_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (model_id, version, now_ms(), job_id, "evaluated", str(out_dir), pkg["sha256"], pkg["size"],
             int(pkg["signed"]), int(report["simulated_data"]), int(report["conclusive"]), jdumps(report)))


def _to_toml(d: dict, prefix: str = "") -> str:
    """Ghi dict 2 cấp thành TOML đơn giản (đủ cho cấu hình huấn luyện)."""
    lines = []
    scalars = {k: v for k, v in d.items() if not isinstance(v, dict)}
    tables = {k: v for k, v in d.items() if isinstance(v, dict)}
    for k, v in scalars.items():
        lines.append(f"{k} = {_toml_value(v)}")
    for k, v in tables.items():
        lines.append(f"\n[{prefix}{k}]")
        for kk, vv in v.items():
            if isinstance(vv, dict):
                continue
            lines.append(f"{kk} = {_toml_value(vv)}")
    return "\n".join(lines) + "\n"


def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    return json.dumps(str(v), ensure_ascii=False)
