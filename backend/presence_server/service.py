"""Nghiệp vụ của backend, dùng chung cho API HTTP và cầu nối MQTT (không phụ thuộc FastAPI)."""

from __future__ import annotations

import logging
import re
import threading
import uuid
from pathlib import Path
from typing import Protocol

import numpy as np

from presence_ml import contract as C
from presence_ml.threshold import THRESHOLD_PRESENCE_DEFAULTS, baseline_stats, factory_thresholds, thresholds_from_baseline

from .config import Settings
from .db import Database, jdumps, jloads, now_ms
from .samples import BatchError, DeviceClock, LiveBuffer, SessionWriter, apply_clock, parse_batch, rows_to_csv

log = logging.getLogger("presence.service")

LABELS = list(C.CLASSES)
EVENT_KINDS = ("false_alarm", "missed_detection", "note")
DEVICE_PACKAGE_PATH = "/api/device/models/{model_id}/package"


class Publisher(Protocol):
    def publish(self, topic: str, payload: dict | None, retain: bool = False, qos: int = 1) -> bool: ...


class NullPublisher:
    def __init__(self):
        self.sent: list[tuple[str, dict | None, bool]] = []

    def publish(self, topic, payload, retain=False, qos=1):
        self.sent.append((topic, payload, retain))
        return True


class ServiceError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


SAFE_ID = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,95}")
KEEP: object = object()  # "không đổi" cho tham số tùy chọn có thể nhận None


def default_device_config() -> dict:
    thr = factory_thresholds()
    return {
        "presence_model": None,  # None: dùng tham số đề xuất trong gói mô hình
        "presence_threshold": vars(THRESHOLD_PRESENCE_DEFAULTS).copy(),
        "thresholds": thr.as_dict(),
        "baseline": None,
        "prefer_threshold": False,
    }


class PresenceService:
    def __init__(self, settings: Settings, db: Database, publisher: Publisher | None = None):
        self.s = settings
        self.db = db
        self.pub: Publisher = publisher or NullPublisher()
        self.live = LiveBuffer(settings.live_minutes)
        self.writers: dict[str, SessionWriter] = {}
        self.clocks: dict[str, DeviceClock] = {}
        self.lock = threading.RLock()
        self.timers: dict[str, threading.Timer] = {}

    # ------------------------------------------------------------------ topics
    def topic(self, device_id: str, suffix: str) -> str:
        return f"{self.s.topic_prefix}/dev/{device_id}/{suffix}"

    # ------------------------------------------------------------------ devices
    def ensure_device(self, device_id: str) -> dict:
        d = self.db.one("SELECT * FROM devices WHERE device_id=?", (device_id,))
        if d:
            return d
        if not device_id or len(device_id) > 32 or not all(c.isalnum() or c in "-_" for c in device_id):
            raise ServiceError("device_id không hợp lệ")
        self.db.execute("INSERT OR IGNORE INTO devices(device_id, name, created_at, config_json) VALUES (?,?,?,?)",
                        (device_id, device_id, now_ms(), jdumps(default_device_config())))
        return self.db.one("SELECT * FROM devices WHERE device_id=?", (device_id,))

    def device_view(self, d: dict) -> dict:
        st = jloads(d.get("status_json"), {}) or {}
        rec = self.db.one("SELECT session_id, kind, started_at_ms, planned_end_ms FROM sessions WHERE device_id=? "
                          "AND status='recording'", (d["device_id"],))
        return {
            "device_id": d["device_id"], "name": d["name"], "room_id": d["room_id"], "online": bool(d["online"]),
            "last_seen_ms": d["last_seen_ms"], "fw_version": d["fw_version"], "status": st,
            "config": jloads(d.get("config_json"), default_device_config()), "config_rev": d["config_rev"],
            "recording": rec,
        }

    def handle_status(self, device_id: str, status: dict) -> None:
        self.ensure_device(device_id)
        online = bool(status.get("online", True))
        model = (status.get("model") or {}).get("active") or {}
        self.db.execute(
            "UPDATE devices SET status_json=?, online=?, last_seen_ms=?, fw_version=COALESCE(?, fw_version), "
            "active_model_id=?, active_model_version=? WHERE device_id=?",
            (jdumps(status), int(online), now_ms(), status.get("fw"), model.get("model_id"), model.get("version"),
             device_id))

    def handle_event(self, device_id: str, ev: dict) -> None:
        self.ensure_device(device_id)
        kind = str(ev.get("type", "device"))
        self.db.execute("INSERT INTO events(device_id, ts_ms, kind, note, data_json, created_at) VALUES (?,?,?,?,?,?)",
                        (device_id, now_ms(), f"device:{kind}", ev.get("error") or ev.get("phase") or "",
                         jdumps(ev), now_ms()))
        if kind == "model_update":
            rid = ev.get("release_id")
            phase = ev.get("phase")
            status = {"active": "active", "failed": "failed"}.get(phase, phase or "pending")
            if rid:
                self.db.execute("UPDATE releases SET status=?, error=?, detail_json=?, updated_at=? WHERE release_id=? "
                                "AND device_id=?", (status, ev.get("error"), jdumps(ev), now_ms(), rid, device_id))

    def _clock_offset(self, device_id: str, boot_id: int, rx_ms: int, now_dev_ms: int) -> float:
        """Độ lệch uptime -> giờ backend của thiết bị chưa có SNTP; lưu SQLite để qua được lần backend khởi động lại."""
        with self.lock:
            clk = self.clocks.get(device_id)
            if clk is None:
                r = self.db.one("SELECT boot_id, offset_ms, dev_ms FROM device_clock WHERE device_id=?", (device_id,))
                clk = DeviceClock(r["boot_id"], r["offset_ms"], r["dev_ms"]) if r else DeviceClock()
                self.clocks[device_id] = clk
            off = clk.update(boot_id, rx_ms, now_dev_ms)
            self.db.execute("INSERT INTO device_clock(device_id, boot_id, offset_ms, dev_ms) VALUES (?,?,?,?) "
                            "ON CONFLICT(device_id) DO UPDATE SET boot_id=excluded.boot_id, "
                            "offset_ms=excluded.offset_ms, dev_ms=excluded.dev_ms",
                            (device_id, boot_id, off, clk.dev_ms))
            return off

    def handle_samples(self, device_id: str, msg: dict, rx_ms: int | None = None) -> int:
        rx_ms = rx_ms or now_ms()
        try:
            header, rows = parse_batch(msg, rx_ms)
        except BatchError as e:
            log.warning("bad batch from %s: %s", device_id, e)
            return 0
        if header["device_id"] != device_id:
            log.warning("batch device %s on topic of %s", header["device_id"], device_id)
            return 0
        self.ensure_device(device_id)
        if header["epoch"] <= 0:
            apply_clock(rows, self._clock_offset(device_id, header["boot_id"], rx_ms, header["now"]))
        fresh = self.live.add(device_id, rows)
        self.db.execute("UPDATE devices SET last_seen_ms=?, online=1 WHERE device_id=?", (rx_ms, device_id))
        if not fresh:
            return 0
        with self.lock:
            sess = self.db.one("SELECT session_id, started_at_ms FROM sessions WHERE device_id=? AND status='recording'",
                               (device_id,))
            if sess:
                keep = [d for d in fresh if d["ts_ms"] >= sess["started_at_ms"]]
                if keep:
                    w = self._writer(sess["session_id"])
                    w.write(sess["session_id"], keep)
                    self.db.execute("UPDATE sessions SET samples=samples+? WHERE session_id=?",
                                    (len(keep), sess["session_id"]))
        return len(fresh)

    def _writer(self, session_id: str) -> SessionWriter:
        w = self.writers.get(session_id)
        if w is None:
            w = SessionWriter(self.samples_path(session_id))
            self.writers[session_id] = w
        return w

    def samples_path(self, session_id: str) -> Path:
        # Mã phiên thành tên thư mục: chỉ ký tự an toàn (chặn "../" từ dữ liệu nhập vào)
        if not SAFE_ID.fullmatch(session_id):
            raise ServiceError(f"mã phiên không hợp lệ: {session_id!r}")
        return self.s.sessions_dir / session_id / "samples.csv"

    def set_device(self, device_id: str, name: str | None = None, room_id: str | None = None) -> dict:
        self.ensure_device(device_id)
        if room_id:
            self.ensure_room(room_id)
        self.db.execute("UPDATE devices SET name=COALESCE(?, name), room_id=COALESCE(?, room_id) WHERE device_id=?",
                        (name, room_id, device_id))
        return self.device_view(self.db.one("SELECT * FROM devices WHERE device_id=?", (device_id,)))

    def ensure_room(self, room_id: str, name: str | None = None) -> None:
        self.db.execute("INSERT OR IGNORE INTO rooms(room_id, name, created_at) VALUES (?,?,?)",
                        (room_id, name or room_id, now_ms()))

    # ------------------------------------------------------------------ device config (desired state)
    def update_device_config(self, device_id: str, patch: dict) -> dict:
        d = self.ensure_device(device_id)
        cfg = jloads(d.get("config_json"), default_device_config())
        for key in ("presence_model", "presence_threshold"):
            if key in patch:
                p = patch[key]
                if p is not None:
                    pp = C.PresenceParams(float(p["enter"]), float(p["exit"]), int(p["confirm_occupied_ms"]),
                                          int(p["confirm_empty_ms"]))
                    try:
                        pp.validate()
                    except ValueError as e:
                        raise ServiceError(f"{key}: {e}") from e
                    cfg[key] = vars(pp)
                else:
                    cfg[key] = None
        if "prefer_threshold" in patch:
            cfg["prefer_threshold"] = bool(patch["prefer_threshold"])
        rev = int(d["config_rev"] or 0) + 1
        self.db.execute("UPDATE devices SET config_json=?, config_rev=? WHERE device_id=?", (jdumps(cfg), rev, device_id))
        self.publish_config(device_id)
        return cfg

    def publish_config(self, device_id: str) -> None:
        d = self.db.one("SELECT config_json, config_rev FROM devices WHERE device_id=?", (device_id,))
        cfg = jloads(d["config_json"], default_device_config())
        self.pub.publish(self.topic(device_id, "desired/config"), {"rev": d["config_rev"], **cfg}, retain=True)

    # ------------------------------------------------------------------ sessions
    def start_session(self, device_id: str, kind: str = "collect", room_id: str | None = None, notes: str = "",
                      duration_s: float | None = None) -> dict:
        d = self.ensure_device(device_id)
        if kind not in ("collect", "baseline"):
            raise ServiceError("kind phải là collect hoặc baseline")
        with self.lock:
            if self.db.one("SELECT 1 FROM sessions WHERE device_id=? AND status='recording'", (device_id,)):
                raise ServiceError("thiết bị đang có phiên thu khác, hãy dừng trước", 409)
            sid = f"{device_id}-{now_ms()}-{uuid.uuid4().hex[:4]}"
            st = jloads(d.get("status_json"), {}) or {}
            room = room_id or d["room_id"]
            if room:
                self.ensure_room(room)
            start = now_ms()
            planned = start + int(duration_s * 1000) if duration_s else None
            self.db.execute(
                "INSERT INTO sessions(session_id, device_id, room_id, kind, source, started_at_ms, status, notes, "
                "radar_json, planned_end_ms) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (sid, device_id, room, kind, "device", start, "recording", notes, jdumps(st.get("radar") or {}),
                 planned))
        return self.session_view(sid)

    def stop_session(self, session_id: str) -> dict:
        with self.lock:
            s = self.db.one("SELECT * FROM sessions WHERE session_id=?", (session_id,))
            if not s:
                raise ServiceError("không có phiên", 404)
            if s["status"] != "recording":
                return self.session_view(session_id)
            end = now_ms()
            self.db.execute("UPDATE sessions SET status='done', ended_at_ms=? WHERE session_id=?", (end, session_id))
            self.db.execute("UPDATE labels SET end_ms=?, updated_at=? WHERE session_id=? AND end_ms IS NULL",
                            (end, end, session_id))
            w = self.writers.pop(session_id, None)
            if w:
                w.close()
            t = self.timers.pop(session_id, None)
            if t:
                t.cancel()
        return self.session_view(session_id)

    def session_view(self, session_id: str) -> dict:
        s = self.db.one("SELECT * FROM sessions WHERE session_id=?", (session_id,))
        if not s:
            raise ServiceError("không có phiên", 404)
        s["radar"] = jloads(s.pop("radar_json"), {})
        s["labels"] = self.db.all("SELECT * FROM labels WHERE session_id=? ORDER BY start_ms", (session_id,))
        return s

    def list_sessions(self, device_id: str | None = None) -> list[dict]:
        q = "SELECT * FROM sessions" + (" WHERE device_id=?" if device_id else "") + " ORDER BY started_at_ms DESC"
        rows = self.db.all(q, (device_id,) if device_id else ())
        for r in rows:
            r["radar"] = jloads(r.pop("radar_json"), {})
            r["label_count"] = self.db.one("SELECT COUNT(*) n FROM labels WHERE session_id=?", (r["session_id"],))["n"]
        return rows

    def update_session(self, session_id: str, notes: str | None = None, split: object = KEEP,
                       room_id: str | None = None) -> dict:
        """Chỉ đổi trường được truyền; split=None/""/"auto" trả phiên về chia tập tự động."""
        self.session_view(session_id)
        if notes is not None:
            self.db.execute("UPDATE sessions SET notes=? WHERE session_id=?", (notes, session_id))
        if room_id is not None:
            self.db.execute("UPDATE sessions SET room_id=? WHERE session_id=?", (room_id, session_id))
        if split is not KEEP:
            if split not in (None, "", "auto", "train", "val", "test"):
                raise ServiceError("split phải là auto/train/val/test")
            self.db.execute("UPDATE sessions SET split=? WHERE session_id=?",
                            (None if split in (None, "", "auto") else split, session_id))
        return self.session_view(session_id)

    def delete_session(self, session_id: str) -> None:
        s = self.db.one("SELECT status FROM sessions WHERE session_id=?", (session_id,))
        if not s:
            raise ServiceError("không có phiên", 404)
        if s["status"] == "recording":
            raise ServiceError("hãy dừng phiên trước khi xóa", 409)
        self.db.execute("DELETE FROM labels WHERE session_id=?", (session_id,))
        self.db.execute("DELETE FROM sessions WHERE session_id=?", (session_id,))
        p = self.samples_path(session_id)
        if p.exists():
            p.unlink()

    # ------------------------------------------------------------------ labels (human-confirmed only)
    def label_now(self, session_id: str, label: str, note: str = "") -> dict:
        """Trong lúc thu: đóng khoảng nhãn đang mở tại lúc này và mở khoảng mới (label='unlabeled' chỉ đóng)."""
        if label not in LABELS + [C.UNLABELED]:
            raise ServiceError("nhãn không hợp lệ")
        s = self.db.one("SELECT status FROM sessions WHERE session_id=?", (session_id,))
        if not s or s["status"] != "recording":
            raise ServiceError("phiên không đang thu", 409)
        t = now_ms()
        with self.db.transaction():
            self.db.conn.execute("UPDATE labels SET end_ms=?, updated_at=? WHERE session_id=? AND end_ms IS NULL",
                                 (t, t, session_id))
            if label != C.UNLABELED:
                self.db.conn.execute("INSERT INTO labels(session_id, start_ms, end_ms, label, source, note, created_at, "
                                     "updated_at) VALUES (?,?,?,?,?,?,?,?)",
                                     (session_id, t, None, label, "human", note, t, t))
        return self.session_view(session_id)

    def set_label(self, session_id: str, start_ms: int, end_ms: int, label: str, note: str = "") -> dict:
        """Gán (hoặc xóa, label='unlabeled') nhãn cho [start, end): cắt các khoảng cũ chồng lên, không để xung đột."""
        if label not in LABELS + [C.UNLABELED]:
            raise ServiceError("nhãn không hợp lệ")
        if end_ms <= start_ms:
            raise ServiceError("end_ms phải lớn hơn start_ms")
        s = self.db.one("SELECT * FROM sessions WHERE session_id=?", (session_id,))
        if not s:
            raise ServiceError("không có phiên", 404)
        t = now_ms()
        with self.db.transaction():
            rows = [dict(r) for r in self.db.conn.execute(
                "SELECT * FROM labels WHERE session_id=? ORDER BY start_ms", (session_id,)).fetchall()]
            for r in rows:
                r_end = r["end_ms"] if r["end_ms"] is not None else 2 ** 62
                if r_end <= start_ms or r["start_ms"] >= end_ms:
                    continue
                self.db.conn.execute("DELETE FROM labels WHERE label_id=?", (r["label_id"],))
                if r["start_ms"] < start_ms:
                    self._ins(session_id, r["start_ms"], start_ms, r["label"], r["note"], r["created_at"], t)
                if r_end > end_ms:
                    self._ins(session_id, end_ms, r["end_ms"], r["label"], r["note"], r["created_at"], t)
            if label != C.UNLABELED:
                self._ins(session_id, start_ms, end_ms, label, note, t, t)
        return self.session_view(session_id)

    def _ins(self, sid, a, b, label, note, created, updated):
        self.db.conn.execute("INSERT INTO labels(session_id, start_ms, end_ms, label, source, note, created_at, "
                             "updated_at) VALUES (?,?,?,?,?,?,?,?)", (sid, a, b, label, "human", note, created, updated))

    def delete_label(self, label_id: int) -> None:
        self.db.execute("DELETE FROM labels WHERE label_id=?", (label_id,))

    # ------------------------------------------------------------------ false alarm / missed detection marks
    def mark_event(self, device_id: str, kind: str, ts_ms: int | None = None, note: str = "") -> dict:
        if kind not in EVENT_KINDS:
            raise ServiceError("kind phải là false_alarm, missed_detection hoặc note")
        self.ensure_device(device_id)
        ts = ts_ms or now_ms()
        rec = self.db.one("SELECT session_id FROM sessions WHERE device_id=? AND status='recording'", (device_id,))
        session_id = rec["session_id"] if rec else None
        snapshot = None
        if session_id is None and kind != "note":
            # Không có phiên đang thu: lưu đoạn dữ liệu gần đây thành phiên "event" CHƯA gán nhãn để gán sau
            rows = self.live.window(device_id, ts - self.s.event_snapshot_minutes * 60_000, ts + 60_000)
            if rows:
                snapshot = f"{device_id}-ev{ts}"
                p = self.samples_path(snapshot)
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(rows_to_csv(snapshot, rows), encoding="utf-8")
                d = self.db.one("SELECT * FROM devices WHERE device_id=?", (device_id,))
                st = jloads(d.get("status_json"), {}) or {}
                self.db.execute(
                    "INSERT INTO sessions(session_id, device_id, room_id, kind, source, started_at_ms, ended_at_ms, "
                    "status, notes, radar_json, samples) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (snapshot, device_id, d["room_id"], "event", "device", rows[0]["ts_ms"], rows[-1]["ts_ms"],
                     "done", f"Trích quanh {kind} lúc {ts}. {note}".strip(), jdumps(st.get("radar") or {}),
                     len(rows)))
                session_id = snapshot
        cur = self.db.execute(
            "INSERT INTO events(device_id, session_id, ts_ms, kind, note, data_json, created_at) VALUES (?,?,?,?,?,?,?)",
            (device_id, session_id, ts, kind, note, jdumps({"snapshot": snapshot}), now_ms()))
        return {"event_id": cur.lastrowid, "session_id": session_id, "snapshot_session": snapshot}

    def list_events(self, device_id: str | None = None, limit: int = 200) -> list[dict]:
        q = "SELECT * FROM events" + (" WHERE device_id=?" if device_id else "") + " ORDER BY ts_ms DESC LIMIT ?"
        rows = self.db.all(q, ((device_id, limit) if device_id else (limit,)))
        for r in rows:
            r["data"] = jloads(r.pop("data_json"), {})
        return rows

    # ------------------------------------------------------------------ "Học phòng trống"
    def start_baseline(self, device_id: str, name: str, condition: str, disturbance: bool, duration_s: float,
                       confirm_empty: bool) -> dict:
        if not confirm_empty:
            raise ServiceError("cần xác nhận phòng KHÔNG có người trước khi học nền")
        if not (30 <= duration_s <= 3600):
            raise ServiceError("thời lượng 30..3600 s")
        s = self.start_session(device_id, "baseline", notes=f"Học phòng trống: {name}. {condition}".strip(),
                               duration_s=duration_s)
        label = "empty_disturbance" if disturbance else "empty_quiet"
        start = s["started_at_ms"]
        self.db.execute("INSERT INTO labels(session_id, start_ms, end_ms, label, source, note, created_at, updated_at) "
                        "VALUES (?,?,?,?,?,?,?,?)", (s["session_id"], start, None, label, "human",
                                                     "người dùng xác nhận phòng trống (học nền)", start, start))
        d = self.db.one("SELECT room_id FROM devices WHERE device_id=?", (device_id,))
        cur = self.db.execute(
            "INSERT INTO baselines(device_id, room_id, session_id, name, condition, disturbance, created_at, status) "
            "VALUES (?,?,?,?,?,?,?,?)", (device_id, d["room_id"], s["session_id"], name, condition, int(disturbance),
                                         now_ms(), "recording"))
        bid = cur.lastrowid
        t = threading.Timer(duration_s, self._finish_baseline_safe, args=(bid,))
        t.daemon = True
        self.timers[s["session_id"]] = t
        t.start()
        return self.baseline_view(bid)

    def _finish_baseline_safe(self, baseline_id: int) -> None:
        try:
            self.finish_baseline(baseline_id)
        except Exception:  # noqa: BLE001
            log.exception("baseline %s", baseline_id)

    def finish_baseline(self, baseline_id: int) -> dict:
        b = self.db.one("SELECT * FROM baselines WHERE baseline_id=?", (baseline_id,))
        if not b:
            raise ServiceError("không có phiên nền", 404)
        self.timers.pop(b["session_id"], None)
        self.stop_session(b["session_id"])
        import pandas as pd

        p = self.samples_path(b["session_id"])
        warnings = []
        if not p.exists():
            self.db.execute("UPDATE baselines SET status='failed', warnings_json=? WHERE baseline_id=?",
                            (jdumps(["không nhận được mẫu nào"]), baseline_id))
            return self.baseline_view(baseline_id)
        df = pd.read_csv(p)
        sess = self.db.one("SELECT * FROM sessions WHERE session_id=?", (b["session_id"],))
        expected = max(1, (sess["ended_at_ms"] - sess["started_at_ms"]) // 200)
        if len(df) < 0.9 * expected:
            warnings.append(f"chỉ nhận {len(df)}/{expected} mẫu: kết nối hoặc radar bị gián đoạn")
        if len(df) < 50:
            self.db.execute("UPDATE baselines SET status='failed', warnings_json=? WHERE baseline_id=?",
                            (jdumps(warnings + ["quá ít mẫu"]), baseline_id))
            return self.baseline_view(baseline_id)
        e = df[list(C.CHANNELS)].to_numpy()
        stats = baseline_stats(e)
        # Dấu hiệu có người trong lúc học nền: không tự loại, chỉ cảnh báo và để người dùng quyết định
        still = e[:, 9 + 2:]
        frac_high_still = float((still > 60).any(axis=1).mean())
        if frac_high_still > 0.3:
            warnings.append(f"{frac_high_still:.0%} mẫu có năng lượng tĩnh > 60: có thể có người ngồi yên trong phòng")
        if "p_decision" in df.columns and "method" in df.columns:
            model_rows = df[df["method"] == 3]
            if len(model_rows):
                occ = float((model_rows["p_decision"] == 2).mean())
                if occ > 0.2:
                    warnings.append(f"mô hình trên thiết bị kết luận có người {occ:.0%} thời gian học nền")
        if "radar_state" in df.columns:
            st_frac = float(((df["radar_state"] & 2) > 0).mean())
            if st_frac > 0.5:
                warnings.append(f"radar tự báo mục tiêu tĩnh {st_frac:.0%} thời gian")
        thr = thresholds_from_baseline(stats)
        status = "review" if warnings else "ok"
        self.db.execute("UPDATE baselines SET status=?, stats_json=?, thresholds_json=?, warnings_json=? "
                        "WHERE baseline_id=?", (status, jdumps(stats), jdumps(thr.as_dict()), jdumps(warnings),
                                                 baseline_id))
        return self.baseline_view(baseline_id)

    def baseline_view(self, baseline_id: int) -> dict:
        b = self.db.one("SELECT * FROM baselines WHERE baseline_id=?", (baseline_id,))
        if not b:
            raise ServiceError("không có phiên nền", 404)
        for k in ("stats", "thresholds", "warnings"):
            b[k] = jloads(b.pop(f"{k}_json"), None)
        return b

    def list_baselines(self, device_id: str | None = None) -> list[dict]:
        q = "SELECT baseline_id FROM baselines" + (" WHERE device_id=?" if device_id else "") + " ORDER BY created_at DESC"
        return [self.baseline_view(r["baseline_id"]) for r in self.db.all(q, (device_id,) if device_id else ())]

    def activate_baseline(self, baseline_id: int, active: bool, acknowledge_warnings: bool = False) -> dict:
        """Dùng (hoặc thôi dùng) nền cho phương pháp ngưỡng trên thiết bị. Không đổi hợp đồng 72 đặc trưng."""
        b = self.baseline_view(baseline_id)
        if active:
            if b["status"] not in ("ok", "review"):
                raise ServiceError("phiên nền chưa xong hoặc thất bại")
            if b["status"] == "review" and not acknowledge_warnings:
                raise ServiceError("phiên nền có cảnh báo: xem lại và xác nhận (acknowledge_warnings) trước khi dùng")
        d = self.ensure_device(b["device_id"])
        cfg = jloads(d.get("config_json"), default_device_config())
        with self.db.transaction():
            self.db.conn.execute("UPDATE baselines SET active=0 WHERE device_id=?", (b["device_id"],))
            if active:
                self.db.conn.execute("UPDATE baselines SET active=1 WHERE baseline_id=?", (baseline_id,))
                cfg["thresholds"] = b["thresholds"]
                cfg["baseline"] = {"id": baseline_id, "name": b["name"]}
            else:
                cfg["thresholds"] = factory_thresholds().as_dict()
                cfg["baseline"] = None
            self.db.conn.execute("UPDATE devices SET config_json=?, config_rev=config_rev+1 WHERE device_id=?",
                                 (jdumps(cfg), b["device_id"]))
        self.publish_config(b["device_id"])
        return self.baseline_view(baseline_id)

    def delete_baseline(self, baseline_id: int) -> None:
        b = self.baseline_view(baseline_id)
        if b["active"]:
            self.activate_baseline(baseline_id, False)
        self.db.execute("DELETE FROM baselines WHERE baseline_id=?", (baseline_id,))

    # ------------------------------------------------------------------ dataset for training
    def build_dataset(self, path: Path, include_simulated: bool = False) -> dict:
        """dataset.json cho presence_ml: mọi phiên đã xong có ít nhất một nhãn người xác nhận."""
        sessions = []
        for s in self.db.all("SELECT * FROM sessions WHERE status='done' ORDER BY started_at_ms"):
            labels = self.db.all("SELECT start_ms, end_ms, label FROM labels WHERE session_id=? AND end_ms IS NOT NULL "
                                 "AND source='human'", (s["session_id"],))
            p = self.samples_path(s["session_id"])
            if not labels or not p.exists():
                continue
            if s["source"] == "simulated" and not include_simulated:
                continue
            sessions.append({
                "session_id": s["session_id"], "device_id": s["device_id"], "room_id": s["room_id"] or "",
                "kind": s["kind"], "source": s["source"], "started_at_ms": s["started_at_ms"],
                "ended_at_ms": s["ended_at_ms"], "samples_path": str(p), "labels": labels, "split": s["split"],
                "notes": s["notes"], "radar": jloads(s["radar_json"], {}),
            })
        baselines = [{"baseline_id": b["baseline_id"], "device_id": b["device_id"], "name": b["name"],
                      "condition": b["condition"], "active": bool(b["active"]), "stats": b["stats"]}
                     for b in self.list_baselines() if b["stats"]]
        ds = {"info": {"created_ms": now_ms(), "source": "backend"}, "sessions": sessions, "baselines": baselines}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(jdumps(ds), encoding="utf-8")
        return ds

    def import_dataset(self, dataset_path: Path) -> list[str]:
        """Nhập bộ dữ liệu (ví dụ mô phỏng) vào backend: phiên + nhãn + file mẫu."""
        import json
        import shutil

        from presence_ml.dataio import load_dataset

        ds = load_dataset(dataset_path)
        raw = json.loads(Path(dataset_path if str(dataset_path).endswith(".json") else
                              Path(dataset_path) / "dataset.json").read_text(encoding="utf-8"))
        ids = []
        for s, rs in zip(ds.sessions, raw["sessions"]):
            if self.db.one("SELECT 1 FROM sessions WHERE session_id=?", (s.session_id,)):
                continue
            self.ensure_device(s.device_id)
            if s.room_id:
                self.ensure_room(s.room_id)
            dst = self.samples_path(s.session_id)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(s.samples_path, dst)
            n = sum(1 for _ in open(dst, encoding="utf-8")) - 1
            self.db.execute(
                "INSERT INTO sessions(session_id, device_id, room_id, kind, source, started_at_ms, ended_at_ms, status, "
                "notes, radar_json, split, samples) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (s.session_id, s.device_id, s.room_id, s.kind, s.source, s.started_at_ms, rs.get("ended_at_ms"), "done",
                 s.notes, jdumps(s.radar), s.split, n))
            for iv in s.labels:
                self._ins(s.session_id, iv.start_ms, iv.end_ms, iv.label, "nhập từ dataset", now_ms(), now_ms())
            ids.append(s.session_id)
        return ids

    # ------------------------------------------------------------------ models and releases
    def model_view(self, m: dict) -> dict:
        m = dict(m)
        m["summary"] = jloads(m.pop("summary_json"), {})
        return m

    def list_models(self) -> list[dict]:
        out = []
        for m in self.db.all("SELECT * FROM models ORDER BY model_version DESC"):
            v = self.model_view(m)
            s = v.pop("summary")
            v["evaluation_summary"] = {
                "insufficient_data": s.get("insufficient_data"),
                "timeline": s.get("evaluation", {}).get("timeline", {}),
                "test_windows": s.get("evaluation", {}).get("windows", {}).get("test", {}).get("int8", {}).get("accuracy"),
            }
            out.append(v)
        return out

    def get_model(self, model_id: str) -> dict:
        m = self.db.one("SELECT * FROM models WHERE model_id=?", (model_id,))
        if not m:
            raise ServiceError("không có mô hình", 404)
        return self.model_view(m)

    def approve_model(self, model_id: str, note: str = "", acknowledge_insufficient: bool = False) -> dict:
        m = self.get_model(model_id)
        if m["status"] not in ("evaluated", "approved"):
            raise ServiceError("chỉ duyệt được mô hình đã có kết quả đánh giá")
        if not (Path(m["dir"]) / "report.json").exists():
            raise ServiceError("thiếu report.json: chưa có kết quả đánh giá")
        if (m["simulated"] or not m["conclusive"]) and not acknowledge_insufficient:
            raise ServiceError("mô hình dùng dữ liệu mô phỏng hoặc chưa đủ dữ liệu kết luận: cần xác nhận "
                               "acknowledge_insufficient để duyệt (chỉ cho thử nghiệm)")
        self.db.execute("UPDATE models SET status='approved', approved_at=?, note=? WHERE model_id=?",
                        (now_ms(), note, model_id))
        return self.get_model(model_id)

    def reject_model(self, model_id: str, note: str = "") -> dict:
        self.get_model(model_id)
        self.db.execute("UPDATE models SET status='rejected', note=? WHERE model_id=?", (note, model_id))
        return self.get_model(model_id)

    def release(self, model_id: str, device_id: str, kind: str = "release") -> dict:
        m = self.get_model(model_id)
        if m["status"] != "approved":
            raise ServiceError("chỉ phát hành mô hình đã được duyệt sau khi xem báo cáo đánh giá")
        self.ensure_device(device_id)
        pkg = Path(m["dir"]) / "model.pkg"
        if not pkg.exists():
            raise ServiceError("thiếu file gói mô hình", 500)
        t = now_ms()
        self.db.execute("UPDATE releases SET status='superseded', updated_at=? WHERE device_id=? AND status IN "
                        "('pending','download','verify','selftest')", (t, device_id))
        cur = self.db.execute("INSERT INTO releases(model_id, device_id, kind, created_at, updated_at, status) "
                              "VALUES (?,?,?,?,?,?)", (model_id, device_id, kind, t, t, "pending"))
        rid = cur.lastrowid
        self.pub.publish(self.topic(device_id, "desired/model"), {
            "model_id": model_id, "model_version": m["model_version"], "size": m["package_size"],
            "sha256": m["package_sha256"], "release_id": rid, "path": DEVICE_PACKAGE_PATH.format(model_id=model_id),
        }, retain=True)
        return self.db.one("SELECT * FROM releases WHERE release_id=?", (rid,))

    def rollback(self, device_id: str) -> dict:
        """Phát hành lại mô hình trước đó của thiết bị (thiết bị dùng lại vùng A/B còn giữ, không cần tải)."""
        d = self.ensure_device(device_id)
        st = jloads(d.get("status_json"), {}) or {}
        prev = ((st.get("model") or {}).get("previous") or {})
        model_id = prev.get("model_id")
        if not model_id:
            rel = self.db.all("SELECT model_id FROM releases WHERE device_id=? AND status='active' ORDER BY release_id "
                              "DESC LIMIT 2", (device_id,))
            model_id = rel[1]["model_id"] if len(rel) > 1 else None
        if not model_id or not self.db.one("SELECT 1 FROM models WHERE model_id=?", (model_id,)):
            raise ServiceError("không biết mô hình trước đó của thiết bị", 404)
        status = self.get_model(model_id)["status"]
        if status != "approved":
            # Không tự duyệt lại: mô hình trước có thể đã bị người dùng từ chối sau khi chạy
            raise ServiceError(f"mô hình trước ({model_id}) đang ở trạng thái '{status}': duyệt lại ở tab Mô hình "
                               "rồi khôi phục", 409)
        return self.release(model_id, device_id, kind="rollback")

    def clear_model(self, device_id: str) -> None:
        """Xóa mô hình mong muốn (thiết bị giữ mô hình đang chạy; không dùng để tắt mô hình)."""
        self.pub.publish(self.topic(device_id, "desired/model"), None, retain=True)

    def list_releases(self, device_id: str | None = None) -> list[dict]:
        q = "SELECT * FROM releases" + (" WHERE device_id=?" if device_id else "") + " ORDER BY release_id DESC"
        rows = self.db.all(q, (device_id,) if device_id else ())
        for r in rows:
            r["detail"] = jloads(r.pop("detail_json"), None)
        return rows

    def send_command(self, device_id: str, cmd: str) -> dict:
        if cmd not in ("reboot", "radar_reinit", "ping", "republish"):
            raise ServiceError("lệnh không hỗ trợ")
        cid = uuid.uuid4().hex[:8]
        self.pub.publish(self.topic(device_id, "cmd"), {"id": cid, "cmd": cmd}, retain=False)
        return {"id": cid, "cmd": cmd}

    # ------------------------------------------------------------------ session data for the label editor
    def session_samples(self, session_id: str, step: int = 1) -> dict:
        import pandas as pd

        p = self.samples_path(session_id)
        if not p.exists():
            return {"columns": [], "rows": []}
        df = pd.read_csv(p)
        if step > 1:
            df = df.iloc[::step]
        cols = ["ts_ms", "seq", "tick", "boot_id", *C.CHANNELS, "ev", "method", "model_version", "pred", "occ_pm",
                "p_state", "p_decision", "radar_state"]
        cols = [c for c in cols if c in df.columns]
        return {"columns": cols, "rows": df[cols].astype(np.int64).values.tolist()}
