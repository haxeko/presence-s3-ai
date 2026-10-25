"""Thiết bị giả lập (khi chưa có ESP32-S3/LD2410): cùng chủ đề MQTT, định dạng dữ liệu và quy trình cập nhật
mô hình như firmware, để chạy thử toàn bộ luồng thu dữ liệu -> huấn luyện -> phát hành -> thiết bị tải mô hình.

Nguồn dữ liệu radar:
  --scenario quiet|fan|still|moving|mix   khung LD2410 MÔ PHỎNG (presence_ml.sim), đi qua bộ phân tích LD2410
  --capture file.ld2410.log               phát lại luồng byte đã thu bằng tools/ld2410_capture.py

    python tools/fake_device.py --device s3-sim001 --scenario mix --mqtt localhost --server http://localhost:8000

Giới hạn: suy luận dùng TFLite trên máy tính (kernel tham chiếu), không phải TFLM/esp-nn trên ESP32;
không đo được thời gian/bộ nhớ của ESP32. Dữ liệu mô phỏng chỉ kiểm tra pipeline.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
import threading
import time
import urllib.request
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "training"))

from presence_ml import contract as C  # noqa: E402
from presence_ml import ld2410  # noqa: E402
from presence_ml import package as P  # noqa: E402
from presence_ml import sim  # noqa: E402
from presence_ml.presence_logic import DECISION_NAMES, STATE_NAMES, PresenceLogic  # noqa: E402
from presence_ml.sampler import Sampler  # noqa: E402
from presence_ml.threshold import (METHOD_MODEL, METHOD_THRESHOLD_BASELINE, METHOD_THRESHOLD_FACTORY,  # noqa: E402
                                   THRESHOLD_PRESENCE_DEFAULTS, Thresholds, window_score)

SCENARIOS = {
    "quiet": lambda bg: [sim.Segment(1e9, "empty_quiet", [bg])],
    "fan": lambda bg: [sim.Segment(1e9, "empty_disturbance", [bg, sim.Fan(3)])],
    "still": lambda bg: [sim.Segment(1e9, "occupied_still", [bg, sim.PersonStill(2)])],
    "moving": lambda bg: [sim.Segment(1e9, "occupied_moving", [bg, sim.PersonMoving()])],
    "mix": lambda bg: [sim.Segment(60, "empty_quiet", [bg]), sim.Segment(40, "occupied_moving", [bg, sim.PersonMoving()]),
                       sim.Segment(90, "occupied_still", [bg, sim.PersonStill(2), sim.Fan(4)]),
                       sim.Segment(90, "empty_disturbance", [bg, sim.Fan(4)])],
}


class ModelSlots:
    """A/B mô phỏng bằng file: slot_a.pkg, slot_b.pkg, meta.json (ghi meta sau cùng, thay file nguyên tử)."""

    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.meta_path = root / "meta.json"
        self.meta = json.loads(self.meta_path.read_text()) if self.meta_path.exists() else {"active": None, "previous": None,
                                                                                           "slots": {}}

    def save(self):
        tmp = self.meta_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.meta))
        tmp.replace(self.meta_path)

    def inactive(self) -> str:
        return "b" if self.meta["active"] == "a" else "a"


class FakeDevice:
    def __init__(self, a):
        import paho.mqtt.client as mqtt

        self.a = a
        self.device_id = a.device
        self.prefix = f"{a.topic_prefix}/dev/{self.device_id}"
        self.boot_id = random.getrandbits(32)
        self.t0 = time.monotonic()
        self.rng = np.random.default_rng(a.seed)
        self.parser = ld2410.Parser()
        self.sampler = Sampler()
        self.seq = 0
        self.tick = 0
        self.window: list[tuple[int, list[int] | None]] = []
        self.ring: list[list[int]] = []
        self.lock = threading.Lock()
        self.cfg = {"presence_model": None, "presence_threshold": vars(THRESHOLD_PRESENCE_DEFAULTS),
                    "thresholds": Thresholds().as_dict(), "baseline": None, "prefer_threshold": False, "rev": 0}
        self.slots = ModelSlots(Path(a.state_dir) / self.device_id)
        self.model = None  # (info, contract, Int8Model)
        self.logic = PresenceLogic(C.PresenceParams(**vars(THRESHOLD_PRESENCE_DEFAULTS)))
        self.logic_key = None
        self.last = {"ev": 0, "meth": 0, "mver": 0, "pred": -1, "q": [0, 0, 0, 0], "occ": -1, "ps": 0, "pd": 0}
        self.update = {"phase": "idle", "error": "", "model_version": 0}
        self.pub_key = Path(a.pubkey).read_bytes() if a.pubkey else None
        self.mqtt = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=self.device_id)
        if a.mqtt_user:
            self.mqtt.username_pw_set(a.mqtt_user, a.mqtt_pass)
        self.mqtt.will_set(f"{self.prefix}/status", json.dumps({"online": False, "reason": "lwt"}), qos=1, retain=True)
        self.mqtt.on_connect = self.on_connect
        self.mqtt.on_message = self.on_message
        self.connected = False
        self.load_active_model()

    # ------------------------------------------------------------- model
    def load_active_model(self):
        act = self.slots.meta.get("active")
        if not act:
            return
        try:
            self.model = self.check_package((self.slots.root / f"slot_{act}.pkg").read_bytes())
        except Exception as e:  # noqa: BLE001
            print(f"[{self.device_id}] active slot invalid at boot: {e}; rollback")
            prev = self.slots.meta.get("previous")
            self.slots.meta["active"], self.slots.meta["previous"] = prev, None
            self.slots.save()
            self.model = None
            if prev:
                self.load_active_model()

    def check_package(self, data: bytes, sha: str | None = None):
        from presence_ml.export import Int8Model

        info = P.verify_package(data, self.pub_key, sha, radar_gate_cm=75)
        c = info.contract()
        m = Int8Model(info.payload[P.SECTION_TFLITE])
        tv = P.TestVectorSet.unpack(info.payload[P.SECTION_TESTVEC])
        worst = 0
        for v in tv.vectors:
            f = C.compute_features(v.energy, v.valid)
            if np.any(np.abs(f - v.features) > tv.tol_feature_abs + tv.tol_feature_rel * np.abs(v.features)):
                raise P.PackageError("selftest: features")
            out = m(v.input_q[None])[0]
            worst = max(worst, int(np.abs(out.astype(int) - v.output_q.astype(int)).max()))
        if worst > tv.tol_output_q:
            raise P.PackageError(f"selftest: output diff {worst}")
        return info, c, m, {"vectors": len(tv.vectors), "max_output_diff": worst}

    def apply_desired_model(self, d: dict):
        info = self.model[0] if self.model else None
        if info and info.model_version == d["model_version"] and info.sha256 == d["sha256"]:
            return self.event("active", d)
        prev = self.slots.meta.get("previous")
        try:
            if prev and self.slots.meta["slots"].get(prev, {}).get("sha256") == d["sha256"]:
                self.event("verify", d)
                data = (self.slots.root / f"slot_{prev}.pkg").read_bytes()
                slot = prev
            else:
                self.event("download", d)
                url = self.a.server.rstrip("/") + f"/api/device/models/{d['model_id']}/package"
                req = urllib.request.Request(url)
                if self.a.device_token:
                    req.add_header("Authorization", "Bearer " + self.a.device_token)
                data = urllib.request.urlopen(req, timeout=20).read()
                if len(data) != d["size"] or hashlib.sha256(data).hexdigest() != d["sha256"]:
                    raise P.PackageError("sha256 or size differs from the announcement")
                slot = self.slots.inactive()
                (self.slots.root / f"slot_{slot}.pkg").write_bytes(data)
                self.event("verify", d)
            model = self.check_package(data, d["sha256"])
            with self.lock:
                self.slots.meta["slots"][slot] = {"model_id": d["model_id"], "version": d["model_version"],
                                                  "sha256": d["sha256"]}
                if self.slots.meta["active"] != slot:
                    self.slots.meta["previous"] = self.slots.meta["active"]
                self.slots.meta["active"] = slot
                self.slots.save()
                self.model = model
            self.event("active", d, selftest=model[3])
        except Exception as e:  # noqa: BLE001
            self.event("failed", d, error=str(e))

    def event(self, phase, d, error="", selftest=None):
        self.update = {"phase": phase, "error": error, "model_version": d.get("model_version", 0)}
        ev = {"type": "model_update", "phase": phase, "model_version": d.get("model_version"),
              "release_id": d.get("release_id"), "error": error}
        if selftest:
            ev["selftest"] = selftest
        print(f"[{self.device_id}] model update {phase} {error}")
        self.mqtt.publish(f"{self.prefix}/event", json.dumps(ev), qos=1)
        self.publish_status()

    # ------------------------------------------------------------- MQTT
    def on_connect(self, client, userdata, flags, rc, props=None):
        self.connected = not rc.is_failure
        for s in ("desired/config", "desired/model", "cmd"):
            client.subscribe(f"{self.prefix}/{s}", qos=1)
        self.publish_status()

    def on_message(self, client, userdata, msg):
        if not msg.payload:
            return
        d = json.loads(msg.payload)
        if msg.topic.endswith("desired/config"):
            with self.lock:
                self.cfg.update(d)
                self.logic_key = None
            self.publish_status()
        elif msg.topic.endswith("desired/model"):
            threading.Thread(target=self.apply_desired_model, args=(d,), daemon=True).start()
        elif msg.topic.endswith("/cmd") and d.get("cmd") in ("ping", "republish"):
            self.publish_status()

    def publish_status(self):
        info = self.model[0] if self.model else None
        prev = self.slots.meta.get("previous")
        pslot = self.slots.meta["slots"].get(prev) if prev else None
        st = {
            "online": True, "device_id": self.device_id, "fw": "fake-device", "fw_api": 1, "boot_id": self.boot_id,
            "uptime_s": int(time.monotonic() - self.t0), "ip": "fake", "time_synced": True,
            "radar": {"status": "ok", "version": "simulated", "gate_cm": 75},
            "sampler": {"sample_rate_hz": 5.0, "radar_reports_per_s": 10.0},
            "buffer": {"capacity": 1000, "used": len(self.ring), "dropped": 0, "policy": "drop_oldest"},
            "model": {"method": {3: "model", 2: "threshold_baseline", 1: "threshold_factory"}.get(self.last["meth"], "none"),
                      "active": {"model_id": info.model_id, "version": info.model_version,
                                 "simulated_data": info.simulated} if info else None,
                      "previous": {"model_id": pslot["model_id"], "version": pslot["version"]} if pslot else None,
                      "update": self.update},
            "presence": {"state": STATE_NAMES[self.last["ps"]], "decision": DECISION_NAMES[self.last["pd"]],
                         "method": {3: "model", 2: "threshold_baseline", 1: "threshold_factory"}.get(self.last["meth"], "none"),
                         "score": None if self.last["occ"] < 0 else self.last["occ"] / 1000,
                         "params": self.current_params().__dict__, "config_rev": self.cfg.get("rev", 0)},
        }
        self.mqtt.publish(f"{self.prefix}/status", json.dumps(st), qos=1, retain=True)

    # ------------------------------------------------------------- decision pipeline (firmware pipeline.cpp)
    def method(self):
        if self.model and not self.cfg.get("prefer_threshold"):
            return METHOD_MODEL
        return METHOD_THRESHOLD_BASELINE if self.cfg.get("baseline") else METHOD_THRESHOLD_FACTORY

    def current_params(self) -> C.PresenceParams:
        if self.method() == METHOD_MODEL:
            p = self.cfg.get("presence_model") or vars(self.model[1].presence)
        else:
            p = self.cfg.get("presence_threshold") or vars(THRESHOLD_PRESENCE_DEFAULTS)
        return C.PresenceParams(**{k: p[k] for k in ("enter", "exit", "confirm_occupied_ms", "confirm_empty_ms")})

    def on_tick(self, tick: int, energies: list[int] | None) -> bool:
        self.window.append((tick, energies))
        self.window = self.window[-C.WINDOW_SLOTS:]
        if (tick + 1) % C.HOP_SLOTS:
            return False
        meth = self.method()
        key = (meth, self.model[0].model_version if self.model else 0, self.cfg.get("rev"))
        if key != self.logic_key:
            self.logic.set_params(self.current_params())
            self.logic.high_ms = self.logic.low_ms = 0
            self.logic_key = key
        x = np.zeros((C.WINDOW_SLOTS, C.N_CHANNELS), np.int64)
        v = np.zeros(C.WINDOW_SLOTS, bool)
        for t, e in self.window:
            i = C.WINDOW_SLOTS - 1 - (tick - t)
            if 0 <= i < C.WINDOW_SLOTS and e is not None:
                x[i] = e
                v[i] = True
        usable = C.window_is_usable(v)
        q = [0, 0, 0, 0]
        pred = -1
        score = None
        if usable:
            if meth == METHOD_MODEL:
                info, c, m, _ = self.model
                f = C.compute_features(x, v)
                qi = C.quantize_input(C.normalize(f, c.feature_mean, c.feature_std), c.input_scale, c.input_zero_point)
                qo = m(qi[None])[0]
                q = [int(t) for t in qo]
                pred = int(np.argmax(qo))
                score = float(C.occupied_score_from_q(qo, c.output_scale, c.output_zero_point))
            else:
                thr = Thresholds(**self.cfg["thresholds"])
                score = float(window_score(x, v, thr))
        r = self.logic.step(score)
        self.last = {"ev": 1, "meth": meth, "mver": self.model[0].model_version if meth == METHOD_MODEL else 0,
                     "pred": pred, "q": q, "occ": -1 if score is None else int(score * 1000 + 0.5), "ps": r.state,
                     "pd": r.decision}
        return True

    # ------------------------------------------------------------- radar source
    def frames(self):
        """(thời điểm s, bytes) của luồng UART."""
        if self.a.capture:
            for line in Path(self.a.capture).read_text().splitlines():
                if line and not line.startswith("#"):
                    t, h = line.split()
                    yield float(t) / 1000, bytes.fromhex(h)
            return
        bg = sim.RoomBackground()
        segs = SCENARIOS[self.a.scenario](bg)
        bounds = np.cumsum([0.0] + [s.seconds for s in segs])
        t = 0.0
        while True:
            k = min(int(np.searchsorted(bounds, t % bounds[-1] if bounds[-1] < 1e8 else t, side="right") - 1),
                    len(segs) - 1)
            mv, st = sim.scene_energies(segs[k].sources, t, self.rng)
            yield t, sim.frame_for(mv, st)
            t += 0.1 + self.rng.normal(0, 0.008)

    def run(self):
        self.mqtt.connect(self.a.mqtt, self.a.mqtt_port, keepalive=30)
        self.mqtt.loop_start()
        start = time.monotonic()
        # Đồng hồ ảo: epoch lúc bắt đầu + thời gian mô phỏng (đúng giờ thật khi --speed 1)
        epoch0 = int(time.time() * 1000)
        next_tick = C.SAMPLE_PERIOD_MS / 1000
        last_status = 0.0
        batch: list[list[int]] = []
        last_send = 0.0
        for t, data in self.frames():
            delay = t / self.a.speed - (time.monotonic() - start)
            if delay > 0:
                time.sleep(delay)
            while next_tick <= t:
                smp = self.sampler.close_tick(self.tick, int(next_tick * 1000))
                evaluated = self.on_tick(self.tick, smp.energies if smp else None)
                if smp:
                    L = self.last
                    batch.append([self.seq, self.tick, smp.n_frames, smp.target_state, smp.moving_cm, smp.moving_energy,
                                  smp.still_cm, smp.still_energy, smp.detect_cm, *smp.moving, *smp.still, smp.light,
                                  smp.out_pin, 1 if evaluated else 0, L["meth"], L["mver"], L["pred"], *L["q"], L["occ"],
                                  L["ps"], L["pd"]])
                    self.seq += 1
                self.tick += 1
                next_tick = (self.tick + 1) * C.SAMPLE_PERIOD_MS / 1000
            for ev in self.parser.feed(data):
                if isinstance(ev, ld2410.Report):
                    self.sampler.add_frame(ev)
            if batch and t - last_send >= 1.0:
                now_ms = int(next_tick * 1000) - C.SAMPLE_PERIOD_MS  # cuối ô của mẫu mới nhất
                msg = {"v": 1, "dev": self.device_id, "boot": self.boot_id, "t0": 0, "now": now_ms,
                       "epoch": epoch0 + now_ms, "rows": batch}
                self.mqtt.publish(f"{self.prefix}/samples", json.dumps(msg, separators=(",", ":")), qos=1)
                batch = []
                last_send = t
            if t - last_status > 15:
                self.publish_status()
                last_status = t
            if self.a.duration and t > self.a.duration:
                break
        time.sleep(0.5)
        self.mqtt.loop_stop()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--device", default="s3-sim001")
    ap.add_argument("--scenario", default="mix", choices=sorted(SCENARIOS))
    ap.add_argument("--capture", help="file .ld2410.log từ tools/ld2410_capture.py")
    ap.add_argument("--mqtt", default="localhost")
    ap.add_argument("--mqtt-port", type=int, default=1883)
    ap.add_argument("--mqtt-user", default="")
    ap.add_argument("--mqtt-pass", default="")
    ap.add_argument("--topic-prefix", default="presence/v1")
    ap.add_argument("--server", default="http://localhost:8000")
    ap.add_argument("--device-token", default="")
    ap.add_argument("--pubkey", default=str(ROOT / "firmware" / "keys" / "model_signing_pub.pem"),
                    help="khóa công khai kiểm chữ ký gói ('' = không bắt buộc ký)")
    ap.add_argument("--state-dir", default=str(ROOT / "data" / "fake_devices"))
    ap.add_argument("--speed", type=float, default=1.0, help="1 = thời gian thực")
    ap.add_argument("--duration", type=float, default=0, help="giây (0 = chạy mãi)")
    ap.add_argument("--seed", type=int, default=1)
    a = ap.parse_args()
    if a.pubkey and not Path(a.pubkey).exists():
        print(f"Không có khóa công khai {a.pubkey}: chạy 'python tools/keys.py init' hoặc --pubkey ''")
        sys.exit(2)
    FakeDevice(a).run()


if __name__ == "__main__":
    main()
