"""Chạy luồng tối thiểu qua API của backend đang chạy, và kiểm tra từng bước:

  thiết bị (ESP32 thật, QEMU hoặc tools/fake_device.py) gửi mẫu -> phiên thu + nhãn -> nhập thêm dữ liệu MÔ PHỎNG
  -> huấn luyện -> báo cáo đánh giá -> duyệt -> phát hành -> thiết bị tải, kiểm tra, chạy thử, kích hoạt mô hình.

    python tools/e2e_minimal_flow.py --server http://localhost:8000 --admin-token ... --device s3-sim001

Dữ liệu mô phỏng chỉ chứng minh pipeline chạy; mô hình tạo ra KHÔNG dùng cho thiết bị thật.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "training"))


class Api:
    def __init__(self, base: str, token: str):
        self.base = base.rstrip("/") + "/api"
        self.token = token

    def __call__(self, method: str, path: str, body=None, raw=False):
        req = urllib.request.Request(self.base + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None)
        req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", "Bearer " + self.token)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = r.read()
        except urllib.error.HTTPError as e:
            raise SystemExit(f"{method} {path}: HTTP {e.code} {e.read().decode(errors='replace')}") from e
        return data.decode() if raw else (json.loads(data) if data else None)


def step(msg: str):
    print(f"\n== {msg}", flush=True)


def wait(what: str, fn, timeout: float, every: float = 2.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        r = fn()
        if r:
            return r
        time.sleep(every)
    raise SystemExit(f"HẾT GIỜ chờ: {what}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server", default="http://localhost:8000")
    ap.add_argument("--admin-token", default="")
    ap.add_argument("--device", required=True)
    ap.add_argument("--collect-seconds", type=float, default=30, help="thời gian thu phiên có nhãn từ thiết bị")
    ap.add_argument("--sim-minutes", type=float, default=60)
    ap.add_argument("--epochs", type=int, default=60)
    ap.add_argument("--update-timeout", type=float, default=180)
    ap.add_argument("--sim-dir", default=str(ROOT / "data" / "sim_e2e"), help="nơi ghi dữ liệu mô phỏng")
    ap.add_argument("--sim-dir-on-server", default="", help="đường dẫn của --sim-dir nhìn từ backend (Docker: /data/sim_e2e)")
    a = ap.parse_args()
    api = Api(a.server, a.admin_token)
    results = {}

    step("1. Backend và thiết bị")
    print(api("GET", "/health"))
    dev = wait("thiết bị gửi trạng thái", lambda: next((d for d in api("GET", "/devices")
                                                         if d["device_id"] == a.device and d["online"]), None), 120)
    st = dev["status"]
    print(f"thiết bị {a.device}: fw {dev['fw_version']}, radar {st.get('radar', {}).get('status')}, "
          f"phương pháp {st.get('presence', {}).get('method')}, mô hình {st.get('model', {}).get('active')}")
    live = wait("mẫu trực tiếp", lambda: api("GET", f"/devices/{a.device}/live?cursor=0")["rows"], 60)
    results["live_samples"] = len(live)
    print(f"{len(live)} mẫu trực tiếp, mẫu cuối: m={[live[-1][f'm{g}'] for g in range(9)]}")

    step("2. Phiên thu có nhãn từ thiết bị")
    s = api("POST", "/sessions", {"device_id": a.device, "notes": "e2e: thu thử"})
    api("POST", f"/sessions/{s['session_id']}/label-now", {"label": "empty_quiet"})
    time.sleep(a.collect_seconds)
    s = api("POST", f"/sessions/{s['session_id']}/stop")
    print(f"phiên {s['session_id']}: {s['samples']} mẫu, nhãn {[x['label'] for x in s['labels']]}")
    results["collected_samples"] = s["samples"]
    assert s["samples"] > 0, "phiên không nhận được mẫu"

    step("3. Nhập dữ liệu MÔ PHỎNG có nhãn (đủ 4 lớp để huấn luyện thử)")
    from presence_ml.sim import generate_dataset

    ds = generate_dataset(Path(a.sim_dir), minutes=a.sim_minutes, seed=7)
    server_path = str(Path(a.sim_dir_on_server) / "dataset.json") if a.sim_dir_on_server else str(ds.resolve())
    print(api("POST", "/datasets/import", {"path": server_path}))

    step("4. Huấn luyện nền")
    job = api("POST", "/jobs", {"include_simulated": True, "overrides": {"model": {"epochs": a.epochs}}})

    def job_done():
        j = api("GET", f"/jobs/{job['job_id']}")
        p = j.get("progress") or {}
        print(f"  tác vụ {j['job_id']}: {j['status']} {p.get('stage', '')} {p.get('epoch', '')}", flush=True)
        return j if j["status"] in ("done", "failed") else None

    j = wait("huấn luyện xong", job_done, 900, 5)
    if j["status"] != "done":
        print(api("GET", f"/jobs/{j['job_id']}/log", raw=True)[-3000:])
        raise SystemExit("huấn luyện thất bại: " + str(j["error"]))
    model_id = j["model_id"]
    m = api("GET", f"/models/{model_id}")
    print(f"mô hình {model_id} v{m['model_version']}: {m['status']}, mô phỏng={m['simulated']}, đủ kết luận={m['conclusive']}")
    print("lý do chưa đủ dữ liệu:", m["summary"]["insufficient_data"][:3], "...")
    results["model"] = {"id": model_id, "version": m["model_version"], "size": m["package_size"]}

    step("5. Không phát hành được khi chưa duyệt; duyệt có xác nhận (vì dữ liệu mô phỏng)")
    try:
        api("POST", "/releases", {"model_id": model_id, "device_id": a.device})
        raise SystemExit("LỖI: phát hành được mô hình chưa duyệt")
    except SystemExit as e:
        if "LỖI" in str(e):
            raise
        print("đúng: bị từ chối khi chưa duyệt")
    api("POST", f"/models/{model_id}/approve", {"note": "e2e, chỉ thử pipeline", "acknowledge_insufficient": True})

    step("6. Phát hành xuống thiết bị và chờ thiết bị báo đã kích hoạt")
    rel = api("POST", "/releases", {"model_id": model_id, "device_id": a.device})

    def active():
        r = next(x for x in api("GET", f"/releases?device_id={a.device}") if x["release_id"] == rel["release_id"])
        print(f"  phát hành {r['release_id']}: {r['status']} {r.get('error') or ''}", flush=True)
        if r["status"] == "failed":
            raise SystemExit(f"thiết bị từ chối mô hình: {r['error']} {r.get('detail')}")
        return r if r["status"] == "active" else None

    r = wait("thiết bị kích hoạt mô hình", active, a.update_timeout, 3)
    print("chi tiết từ thiết bị:", json.dumps(r.get("detail"), ensure_ascii=False))
    results["release"] = r.get("detail")

    step("7. Thiết bị chạy mô hình mới")

    def running():
        d = next(x for x in api("GET", "/devices") if x["device_id"] == a.device)
        st = d["status"]
        act = (st.get("model") or {}).get("active") or {}
        p = st.get("presence") or {}
        print(f"  mô hình {act.get('model_id')} v{act.get('version')}, phương pháp {p.get('method')}, "
              f"trạng thái {p.get('state')}, điểm {p.get('score')}", flush=True)
        return st if act.get("model_id") == model_id and p.get("method") == "model" else None

    st = wait("trạng thái dùng mô hình", running, 90, 3)
    rows = api("GET", f"/devices/{a.device}/live?cursor=0")["rows"]
    evals = [x for x in rows if x["ev"] == 1 and x["method"] == 3]
    print(f"{len(evals)} quyết định của mô hình trong bộ đệm trực tiếp; ví dụ q={[evals[-1][f'q{k}'] for k in range(4)] if evals else None}")
    results["model_decisions"] = len(evals)
    print("\nKẾT QUẢ:", json.dumps(results, ensure_ascii=False))
    print("E2E OK")


if __name__ == "__main__":
    main()
