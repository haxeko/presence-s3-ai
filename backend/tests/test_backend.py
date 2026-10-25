"""Backend: nhận mẫu, phiên + nhãn, học nền, phát hành/khôi phục, API và xác thực (MQTT thay bằng publisher giả)."""

from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from presence_ml import contract as C
from presence_ml import package as P
from presence_server.config import Settings
from presence_server.db import Database, jdumps, now_ms
from presence_server.main import create_app
from presence_server.service import NullPublisher, PresenceService, ServiceError


def batch(dev="s3-aabbcc", boot=7, start_seq=0, n=5, energy=5, now=None, still=None, method=0, pd_=0):
    rows = []
    for i in range(n):
        seq = start_seq + i
        e = [energy] * 9 + ([still] * 9 if still is not None else [energy] * 9)
        rows.append([seq, seq, 2, 0, 0, 0, 0, 0, 0, *e, -1, -1, 0, method, 0, -1, 0, 0, 0, 0, -1, 0, pd_])
    t_last = 1000 + 200 * (start_seq + n)
    return {"v": 1, "dev": dev, "boot": boot, "t0": 1000, "now": now if now is not None else t_last,
            "epoch": 0, "rows": rows}


@pytest.fixture
def svc(tmp_path):
    s = Settings(data_dir=tmp_path, mqtt_enabled=False)
    for d in (s.sessions_dir, s.models_dir, s.jobs_dir):
        d.mkdir(parents=True, exist_ok=True)
    db = Database(s.db_path)
    pub = NullPublisher()
    service = PresenceService(s, db, pub)
    yield service
    db.close()


def test_batch_parsing_dedup_and_timestamps(svc):
    t = now_ms()
    assert svc.handle_samples("s3-aabbcc", batch(n=5), rx_ms=t) == 5
    assert svc.handle_samples("s3-aabbcc", batch(n=5), rx_ms=t) == 0  # QoS 1 gửi lại: bỏ trùng
    rows = svc.live.since("s3-aabbcc", 0)
    assert len(rows) == 5
    # mẫu cuối kết thúc đúng lúc "now" của thiết bị => ts = thời điểm nhận
    assert rows[-1]["ts_ms"] == t and rows[0]["ts_ms"] == t - 800
    bad = batch(n=1)
    bad["rows"][0] = bad["rows"][0][:10]
    assert svc.handle_samples("s3-aabbcc", bad) == 0
    bad = batch(n=1, energy=101)
    assert svc.handle_samples("s3-aabbcc", bad) == 0
    assert svc.handle_samples("s3-other", batch(n=1)) == 0  # mã thiết bị không khớp chủ đề


def test_clock_without_sntp_survives_backend_restart_and_queued_batches(svc):
    """Thiết bị chưa có SNTP. Backend khởi động lại và broker giao một loạt lô đã xếp hàng (trễ tới 60 s): giờ của các
    mẫu vẫn phải đúng (không lệch theo lúc nhận)."""
    rng = np.random.default_rng(1)
    t_server_at_uptime0 = 1_700_000_000_000

    def send(service, k, delay_ms):
        now = 1000 + 1000 * (k + 1)  # uptime lúc gửi lô k = cuối ô của mẫu cuối trong lô
        service.handle_samples("s3-aabbcc", batch(start_seq=5 * k, n=5, now=now),
                               rx_ms=t_server_at_uptime0 + now + delay_ms)

    for k in range(60):
        send(svc, k, int(rng.integers(30, 80)))
    restarted = PresenceService(svc.s, svc.db, NullPublisher())  # cùng SQLite, bộ nhớ mới
    for k in range(60, 120):
        send(restarted, k, 60_000 - 1000 * (k - 60))  # lô xếp hàng ở broker, đến cùng lúc khi backend chạy lại
    for k in range(120, 130):
        send(restarted, k, int(rng.integers(30, 80)))
    rows = restarted.live.since("s3-aabbcc", 0)
    assert len(rows) == 70 * 5
    for r in rows:
        true_ts = t_server_at_uptime0 + 1000 + 200 * (r["tick"] + 1)
        assert abs(r["ts_ms"] - true_ts) <= 100, r
    assert {r["ts_src"] for r in rows} == {"server"}


def test_device_clock_tracks_crystal_drift():
    from presence_server.samples import DeviceClock

    rng = np.random.default_rng(2)
    clk = DeviceClock()
    for drift_ppm in (-50, 50):
        clk = DeviceClock()
        for i in range(2 * 3600):  # 2 giờ, 1 lô/giây
            true_ms = 1000 * i
            now_dev = int(true_ms * (1 + drift_ppm * 1e-6))
            off = clk.update(7, 1_000_000 + true_ms + int(rng.integers(20, 60)), now_dev)
        # thời điểm gửi lô cuối ước lượng từ uptime: sai số dưới 100 ms sau 2 giờ trôi (+-360 ms nếu không theo)
        assert abs(off + now_dev - (1_000_000 + true_ms)) < 100, (drift_ppm, off + now_dev - 1_000_000 - true_ms)


def test_session_recording_labels_and_dataset(svc, tmp_path):
    s = svc.start_session("s3-aabbcc", notes="thử")
    with pytest.raises(ServiceError):
        svc.start_session("s3-aabbcc")
    base = s["started_at_ms"]
    svc.label_now(s["session_id"], "empty_quiet")
    # Mẫu có thời điểm sau lúc bắt đầu phiên (mẫu trước đó không thuộc phiên)
    svc.handle_samples("s3-aabbcc", batch(n=10, now=200 * 10 + 1000), rx_ms=base + 2000)
    time.sleep(0.01)
    svc.label_now(s["session_id"], "occupied_still")
    svc.handle_samples("s3-aabbcc", batch(start_seq=10, n=10, now=200 * 20 + 1000), rx_ms=base + 4000)
    done = svc.stop_session(s["session_id"])
    assert done["status"] == "done" and done["samples"] == 20
    assert [lb["label"] for lb in done["labels"]] == ["empty_quiet", "occupied_still"]
    assert all(lb["end_ms"] is not None for lb in done["labels"])
    # Sửa nhãn theo khoảng: phần chồng lên bị cắt, không còn xung đột
    a = done["labels"][0]["start_ms"]
    v = svc.set_label(s["session_id"], a, a + 5, "empty_disturbance")
    labs = sorted((lb["start_ms"], lb["end_ms"], lb["label"]) for lb in v["labels"])
    for (s1, e1, _), (s2, _, _) in zip(labs, labs[1:]):
        assert e1 <= s2
    assert labs[0][2] == "empty_disturbance"
    v = svc.set_label(s["session_id"], a, a + 5, "unlabeled")  # xóa nhãn đoạn đó
    assert all(lb["label"] != "empty_disturbance" for lb in v["labels"])
    ds = svc.build_dataset(tmp_path / "ds.json")
    assert len(ds["sessions"]) == 1 and ds["sessions"][0]["samples_path"].endswith("samples.csv")
    with pytest.raises(ServiceError):
        svc.label_now(s["session_id"], "occupied_still")  # phiên đã dừng


def test_import_rejects_unsafe_session_id(svc, tmp_path):
    s = svc.start_session("s3-aabbcc")
    svc.label_now(s["session_id"], "empty_quiet")
    svc.handle_samples("s3-aabbcc", batch(n=10, now=200 * 10 + 1000), rx_ms=s["started_at_ms"] + 2000)
    svc.stop_session(s["session_id"])
    ds_path = tmp_path / "ds.json"
    ds = svc.build_dataset(ds_path)
    ds["sessions"][0]["session_id"] = "../../escape"
    ds_path.write_text(json.dumps(ds), encoding="utf-8")
    with pytest.raises(ServiceError, match="mã phiên"):
        svc.import_dataset(ds_path)
    assert not (svc.s.sessions_dir / "../../escape").exists()


def test_unlabeled_sessions_are_not_training_data(svc, tmp_path):
    s = svc.start_session("s3-aabbcc")
    svc.handle_samples("s3-aabbcc", batch(n=10), rx_ms=now_ms() + 50)
    svc.stop_session(s["session_id"])
    assert svc.build_dataset(tmp_path / "ds.json")["sessions"] == []


def test_baseline_requires_confirmation_and_warns(svc):
    with pytest.raises(ServiceError, match="xác nhận"):
        svc.start_baseline("s3-aabbcc", "quạt tắt", "", False, 60, confirm_empty=False)
    b = svc.start_baseline("s3-aabbcc", "quạt tắt", "đêm", False, 60, confirm_empty=True)
    svc.timers[b["session_id"]].cancel()
    t = now_ms()
    # Năng lượng tĩnh cao kéo dài: dấu hiệu có người ngồi yên trong lúc "học nền"
    for k in range(30):
        svc.handle_samples("s3-aabbcc", batch(start_seq=10 * k, n=10, still=80, now=1000 + 200 * 10 * (k + 1)),
                           rx_ms=t + 2000 * (k + 1))
    r = svc.finish_baseline(b["baseline_id"])
    assert r["status"] == "review" and any("ngồi yên" in w for w in r["warnings"])
    assert r["thresholds"]["still"][3] == 88  # đỉnh 80 + biên tối thiểu 8
    with pytest.raises(ServiceError, match="cảnh báo"):
        svc.activate_baseline(b["baseline_id"], True)
    svc.activate_baseline(b["baseline_id"], True, acknowledge_warnings=True)
    topic, payload, retain = svc.pub.sent[-1]
    assert topic.endswith("/desired/config") and retain and payload["baseline"]["id"] == b["baseline_id"]
    session = svc.session_view(b["session_id"])
    assert session["kind"] == "baseline" and session["labels"][0]["label"] == "empty_quiet"


def _fake_model(svc, model_id="m1", version=1, simulated=0, conclusive=1):
    d = svc.s.models_dir / model_id
    d.mkdir(parents=True)
    (d / "model.pkg").write_bytes(b"PKG" * 10)
    (d / "report.json").write_text("{}", encoding="utf-8")
    svc.db.execute("INSERT INTO models(model_id, model_version, created_at, status, dir, package_sha256, package_size,"
                   " signed, simulated, conclusive, summary_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                   (model_id, version, now_ms(), "evaluated", str(d), "ab" * 32, 30, 1, simulated, conclusive,
                    jdumps({"insufficient_data": [] if conclusive else ["x"]})))


def test_release_requires_approval_and_rollback(svc):
    _fake_model(svc, "m1", 1)
    _fake_model(svc, "m2", 2, simulated=1, conclusive=0)
    with pytest.raises(ServiceError, match="duyệt"):
        svc.release("m1", "s3-aabbcc")
    svc.approve_model("m1")
    with pytest.raises(ServiceError, match="mô phỏng"):
        svc.approve_model("m2")
    svc.approve_model("m2", acknowledge_insufficient=True)
    r1 = svc.release("m1", "s3-aabbcc")
    topic, payload, retain = svc.pub.sent[-1]
    assert topic == "presence/v1/dev/s3-aabbcc/desired/model" and retain
    assert payload["model_version"] == 1 and payload["path"] == "/api/device/models/m1/package"
    svc.handle_event("s3-aabbcc", {"type": "model_update", "phase": "active", "release_id": r1["release_id"],
                                   "model_version": 1})
    r2 = svc.release("m2", "s3-aabbcc")
    svc.handle_event("s3-aabbcc", {"type": "model_update", "phase": "failed", "release_id": r2["release_id"],
                                   "error": "selftest: testvec_mismatch"})
    rel = {r["release_id"]: r for r in svc.list_releases("s3-aabbcc")}
    assert rel[r1["release_id"]]["status"] == "active" and rel[r2["release_id"]]["status"] == "failed"
    svc.handle_status("s3-aabbcc", {"online": True, "model": {"active": {"model_id": "m2", "version": 2},
                                                              "previous": {"model_id": "m1", "version": 1}}})
    rb = svc.rollback("s3-aabbcc")
    assert rb["model_id"] == "m1" and rb["kind"] == "rollback"
    # Mô hình trước đã bị từ chối: khôi phục không được tự duyệt lại nó
    svc.reject_model("m1", note="báo giả nhiều")
    with pytest.raises(ServiceError, match="duyệt lại"):
        svc.rollback("s3-aabbcc")


def test_mark_false_alarm_snapshot(svc):
    t = now_ms()
    svc.handle_samples("s3-aabbcc", batch(n=10), rx_ms=t)
    r = svc.mark_event("s3-aabbcc", "false_alarm", note="quạt")
    assert r["snapshot_session"]
    s = svc.session_view(r["snapshot_session"])
    assert s["kind"] == "event" and s["labels"] == [] and s["samples"] == 10  # chưa gán nhãn: gán sau


# ---------------------------------------------------------------- HTTP API
@pytest.fixture
def client(tmp_path):
    s = Settings(data_dir=tmp_path, mqtt_enabled=False, admin_token="admin-secret", device_tokens=["dev-secret"])
    for d in (s.sessions_dir, s.models_dir, s.jobs_dir):
        d.mkdir(parents=True, exist_ok=True)
    app = create_app(s, start_mqtt=False)
    with TestClient(app) as c:
        yield c, app


def test_api_auth_and_flow(client):
    c, app = client
    assert c.get("/api/health").json()["ok"]
    assert c.get("/api/devices").status_code == 401
    H = {"Authorization": "Bearer admin-secret"}
    svc = app.state.service
    svc.handle_samples("s3-aabbcc", batch(n=5))
    devs = c.get("/api/devices", headers=H).json()
    assert devs[0]["device_id"] == "s3-aabbcc"
    live = c.get("/api/devices/s3-aabbcc/live?cursor=0", headers=H).json()
    assert len(live["rows"]) == 5
    s = c.post("/api/sessions", json={"device_id": "s3-aabbcc", "notes": "a"}, headers=H).json()
    assert c.post(f"/api/sessions/{s['session_id']}/label-now", json={"label": "nope"}, headers=H).status_code == 400
    assert c.post(f"/api/sessions/{s['session_id']}/label-now", json={"label": "occupied_moving"},
                  headers=H).status_code == 200
    assert c.post(f"/api/sessions/{s['session_id']}/stop", headers=H).json()["status"] == "done"
    # PATCH chỉ đổi trường được gửi: sửa ghi chú không làm mất tập cố định
    sid = s["session_id"]
    assert c.patch(f"/api/sessions/{sid}", json={"split": "test"}, headers=H).json()["split"] == "test"
    v = c.patch(f"/api/sessions/{sid}", json={"notes": "quạt số 3"}, headers=H).json()
    assert v["split"] == "test" and v["notes"] == "quạt số 3"
    assert c.patch(f"/api/sessions/{sid}", json={"split": "auto"}, headers=H).json()["split"] is None
    assert c.patch(f"/api/sessions/{sid}", json={"split": "x"}, headers=H).status_code == 400
    assert c.patch("/api/sessions/khong-co", json={"notes": "a"}, headers=H).status_code == 404
    r = c.put("/api/devices/s3-aabbcc/config", json={"presence_threshold": {"enter": 0.2, "exit": 0.4,
              "confirm_occupied_ms": 1000, "confirm_empty_ms": 30000}}, headers=H)
    assert r.status_code == 400  # exit >= enter
    r = c.put("/api/devices/s3-aabbcc/config", json={"presence_threshold": {"enter": 0.6, "exit": 0.2,
              "confirm_occupied_ms": 1000, "confirm_empty_ms": 30000}}, headers=H)
    assert r.status_code == 200
    contract = c.get("/api/contract", headers=H).json()
    assert contract["feature_names"][0] == "mean_m0" and len(contract["feature_names"]) == 72


def test_device_package_download_needs_token_and_approval(client):
    c, app = client
    svc = app.state.service
    _fake_model(svc, "m9", 9)
    assert c.get("/api/device/models/m9/package").status_code == 401
    D = {"Authorization": "Bearer dev-secret"}
    assert c.get("/api/device/models/m9/package", headers=D).status_code == 403  # chưa duyệt
    svc.approve_model("m9")
    r = c.get("/api/device/models/m9/package", headers=D)
    assert r.status_code == 200 and r.content == b"PKG" * 10 and r.headers["x-package-sha256"] == "ab" * 32
