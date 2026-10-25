"""Công cụ radar qua cổng COM (pyserial) chạy trên cặp pty ảo: không cần phần cứng, chỉ chạy trên Linux/macOS."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

pty = pytest.importorskip("pty")
tty = pytest.importorskip("tty")
pytest.importorskip("serial")

REPO = Path(__file__).resolve().parents[2]
from presence_ml import ld2410  # noqa: E402


def make_pty():
    m, s = pty.openpty()
    tty.setraw(s)
    os.set_blocking(m, False)
    return m, s, os.ttyname(s)


def pump(fd, seconds, on_bytes):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            on_bytes(os.read(fd, 4096))
        except BlockingIOError:
            time.sleep(0.01)


def test_replay_over_serial_sends_frames_and_answers_commands():
    m, s, path = make_pty()
    proc = subprocess.Popen([sys.executable, str(REPO / "tools/ld2410_replay.py"), "--serial", path,
                             "--scenario", "still", "--duration", "4"], stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    try:
        time.sleep(1.0)
        parser = ld2410.Parser()
        events = []
        feed = lambda d: events.extend(parser.feed(d))  # noqa: E731
        for cmd, val in ((ld2410.CMD_ENABLE_CONFIG, b"\x01\x00"), (ld2410.CMD_READ_VERSION, b""),
                         (ld2410.CMD_READ_PARAMS, b"")):
            os.write(m, ld2410.build_command(cmd, val))
            pump(m, 0.5, feed)
        pump(m, 1.0, feed)
        out = proc.communicate(timeout=20)[0]
    finally:
        proc.kill()
        os.close(m)
        os.close(s)
    reports = [e for e in events if isinstance(e, ld2410.Report) and e.full_layout]
    acks = {e.command for e in events if isinstance(e, ld2410.Ack)}
    assert proc.returncode == 0, out
    assert len(reports) >= 15
    assert {0x01FF, 0x01A0, 0x0161} <= acks


def test_capture_over_serial_enables_engineering_and_writes_replayable_log(tmp_path):
    m, s, path = make_pty()
    stop = threading.Event()
    commands = []

    def radar():
        t0, k = time.monotonic(), 0
        buf = bytearray()
        while not stop.is_set():
            try:
                buf += os.read(m, 4096)
            except BlockingIOError:
                pass
            while (i := buf.find(ld2410.CMD_HEADER)) >= 0 and len(buf) >= i + 8:
                cmd = buf[i + 6] | (buf[i + 7] << 8)
                commands.append(cmd)
                del buf[: i + 8]
                os.write(m, ld2410.build_ack(cmd, 0, b"\x01\x00\x40\x00" if cmd == ld2410.CMD_ENABLE_CONFIG else b""))
            if time.monotonic() - t0 > k * 0.1:
                e = [min(100, 10 + g) for g in range(9)]
                os.write(m, ld2410.build_report(e, e, target_state=1))
                k += 1
            time.sleep(0.005)

    th = threading.Thread(target=radar, daemon=True)
    th.start()
    log = tmp_path / "test.ld2410.log"
    try:
        r = subprocess.run([sys.executable, str(REPO / "tools/ld2410_capture.py"), "--serial", path, "--out", str(log),
                            "--seconds", "3", "--engineering"], capture_output=True, text=True, timeout=30)
    finally:
        stop.set()
        th.join()
        os.close(m)
        os.close(s)
    assert r.returncode == 0, r.stderr
    assert commands[:3] == [ld2410.CMD_ENABLE_CONFIG, ld2410.CMD_ENGINEERING_ON, ld2410.CMD_END_CONFIG]
    parser = ld2410.Parser()
    n = 0
    for line in log.read_text().splitlines():
        if line and not line.startswith("#"):
            n += sum(1 for e in parser.feed(bytes.fromhex(line.split(" ", 1)[1]))
                     if isinstance(e, ld2410.Report) and e.full_layout)
    assert n >= 15 and parser.stats.bad_footer == 0
