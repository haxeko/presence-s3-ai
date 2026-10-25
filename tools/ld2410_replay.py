"""Phát luồng byte LD2410 vào UART của ESP32 (qua bộ chuyển USB-UART) hoặc vào socket TCP (QEMU UART1),
để thử firmware khi chưa có radar.

    # Kịch bản MÔ PHỎNG (khung Engineering Mode ~10 Hz), 256000 baud ra cổng COM5:
    python tools/ld2410_replay.py --serial COM5 --scenario mix
    # Phát lại đúng luồng byte đã thu từ radar thật:
    python tools/ld2410_replay.py --serial COM5 --capture phong-khach.ld2410.log
    # QEMU: firmware/test/qemu/run_firmware_e2e.sh mở UART1 ở tcp:5556
    python tools/ld2410_replay.py --tcp localhost:5556 --scenario still

Nối dây: TX của bộ USB-UART -> chân RX radar của ESP32 (mặc định GPIO18), GND chung, mức 3,3 V.
Bỏ dây TX radar thật khi phát lại. Firmware gửi lệnh cấu hình (Engineering Mode) ra TX: công cụ trả lời ACK
giả (--ack) để firmware thấy radar "đã bật Engineering Mode".
"""

from __future__ import annotations

import argparse
import socket
import sys
import threading
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "training"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_device import SCENARIOS  # noqa: E402
from presence_ml import ld2410, sim  # noqa: E402


class Port:
    def __init__(self, a):
        self.sock = None
        self.ser = None
        if a.tcp:
            host, port = a.tcp.split(":")
            self.sock = socket.create_connection((host, int(port)))
        else:
            import serial  # pyserial

            self.ser = serial.Serial(a.serial, a.baud, timeout=0.05)

    def write(self, b: bytes):
        if self.sock:
            self.sock.sendall(b)
        else:
            self.ser.write(b)

    def read(self) -> bytes:
        if self.sock:
            self.sock.settimeout(0.05)
            try:
                return self.sock.recv(256)
            except (TimeoutError, socket.timeout):
                return b""
        return self.ser.read(256)


def ack_responder(port: Port, stop: threading.Event):
    """Trả lời lệnh cấu hình như LD2410C (V2.44, 0.75 m/gate). Lệnh: FD FC FB FA | LEN | CMD (2) [giá trị] | 04 03 02 01."""
    replies = {
        ld2410.CMD_ENABLE_CONFIG: bytes([0x01, 0x00, 0x40, 0x00]),
        ld2410.CMD_READ_PARAMS: bytes([0xAA, 8, 8, 8, *ld2410.FACTORY_MOVING, *ld2410.FACTORY_STILL, 5, 0]),
        ld2410.CMD_READ_VERSION: bytes([0x00, 0x00, 0x44, 0x02, 0x17, 0x09, 0x07, 0x25]),
        ld2410.CMD_READ_RESOLUTION: bytes([0x00, 0x00]),
    }
    buf = bytearray()
    while not stop.is_set():
        data = port.read()
        if not data:
            continue
        buf += data
        while True:
            i = buf.find(ld2410.CMD_HEADER)
            if i < 0 or len(buf) < i + 6:
                break
            n = buf[i + 4] | (buf[i + 5] << 8)
            end = i + 6 + n + 4
            if len(buf) < end:
                break
            frame = bytes(buf[i:end])
            del buf[:end]
            if frame[-4:] == ld2410.CMD_FOOTER and n >= 2:
                cmd = frame[6] | (frame[7] << 8)
                port.write(ld2410.build_ack(cmd, 0, replies.get(cmd, b"")))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--serial")
    g.add_argument("--tcp")
    ap.add_argument("--baud", type=int, default=256000)
    ap.add_argument("--scenario", default="mix", choices=sorted(SCENARIOS))
    ap.add_argument("--capture")
    ap.add_argument("--no-ack", dest="ack", action="store_false")
    ap.add_argument("--duration", type=float, default=0)
    ap.add_argument("--silence", type=str, default="", help="khoảng radar im lặng, ví dụ 60:75 (giây)")
    a = ap.parse_args()
    port = Port(a)
    stop = threading.Event()
    if a.ack:
        threading.Thread(target=ack_responder, args=(port, stop), daemon=True).start()
    silence = tuple(float(x) for x in a.silence.split(":")) if a.silence else None
    start = time.monotonic()
    sent = 0
    try:
        if a.capture:
            items = [(float(t) / 1000, bytes.fromhex(h)) for t, h in
                     (ln.split() for ln in Path(a.capture).read_text().splitlines() if ln and not ln.startswith("#"))]
        else:
            rng = np.random.default_rng(1)
            bg = sim.RoomBackground()
            segs = SCENARIOS[a.scenario](bg)
            bounds = np.cumsum([0.0] + [s.seconds for s in segs])

            def gen():
                t = 0.0
                while True:
                    tt = t % bounds[-1] if bounds[-1] < 1e8 else t
                    k = min(int(np.searchsorted(bounds, tt, side="right") - 1), len(segs) - 1)
                    mv, st = sim.scene_energies(segs[k].sources, t, rng)
                    yield t, sim.frame_for(mv, st)
                    t += 0.1

            items = gen()
        for t, data in items:
            delay = t - (time.monotonic() - start)
            if delay > 0:
                time.sleep(delay)
            if silence and silence[0] <= t < silence[1]:
                continue
            port.write(data)
            sent += 1
            if a.duration and t > a.duration:
                break
    except KeyboardInterrupt:
        pass
    stop.set()
    print(f"đã gửi {sent} khung")


if __name__ == "__main__":
    main()
