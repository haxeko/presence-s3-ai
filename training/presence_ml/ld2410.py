"""Giao thức UART của HLK-LD2410 / LD2410B / LD2410C: tạo khung (mô phỏng) và phân tích theo luồng.

Phải giống ``firmware/main/core/ld2410_parser.cpp`` (cùng file fixture kiểm tra cả hai).

Khung báo cáo (radar -> ESP32):
    F4 F3 F2 F1 | LEN (2, LE) | TYPE AA STATE MV_CM(2) MV_E ST_CM(2) ST_E DET_CM(2) [kỹ thuật] 55 00 | F8 F7 F6 F5
    TYPE 0x02 = cơ bản (LEN = 13), 0x01 = Engineering Mode (LEN = 35 với 9 gate + 2 byte phụ).
    Phần kỹ thuật: N (gate chuyển động lớn nhất), M (gate tĩnh lớn nhất), năng lượng chuyển động gate 0..N,
    năng lượng tĩnh gate 0..M, rồi byte phụ (LD2410B/C: giá trị cảm biến ánh sáng, trạng thái chân OUT).
Khung lệnh/ACK:
    FD FC FB FA | LEN (2, LE) | COMMAND (2) [STATUS (2) trong ACK] [giá trị] | 04 03 02 01

Giao thức KHÔNG có CRC: tính toàn vẹn dựa vào header, LEN, footer, byte đầu 0xAA, đuôi 0x55 và byte
kiểm tra 0x00 theo tài liệu. Khung sai được bỏ, bộ phân tích dò lại header từ byte kế tiếp.
"""

from __future__ import annotations

from dataclasses import dataclass, field

REPORT_HEADER = bytes([0xF4, 0xF3, 0xF2, 0xF1])
REPORT_FOOTER = bytes([0xF8, 0xF7, 0xF6, 0xF5])
CMD_HEADER = bytes([0xFD, 0xFC, 0xFB, 0xFA])
CMD_FOOTER = bytes([0x04, 0x03, 0x02, 0x01])
MAX_PAYLOAD = 64
GATES = 9

TYPE_ENGINEERING = 0x01
TYPE_BASIC = 0x02

CMD_ENABLE_CONFIG = 0x00FF
CMD_END_CONFIG = 0x00FE
CMD_ENGINEERING_ON = 0x0062
CMD_ENGINEERING_OFF = 0x0063
CMD_READ_PARAMS = 0x0061
CMD_READ_VERSION = 0x00A0
CMD_READ_RESOLUTION = 0x00AB
ACK_FLAG = 0x0100

# Ngưỡng xuất xưởng của LD2410 (giá trị đọc được từ radar mới, 0.75 m/gate). Tĩnh gate 0-1 không chỉnh được.
FACTORY_MOVING = [50, 50, 40, 30, 20, 15, 15, 15, 15]
FACTORY_STILL = [0, 0, 40, 40, 30, 30, 20, 20, 20]


@dataclass
class Report:
    type: int
    target_state: int
    moving_cm: int
    moving_energy: int
    still_cm: int
    still_energy: int
    detect_cm: int
    engineering: bool = False
    max_moving_gate: int = 0
    max_still_gate: int = 0
    moving: list[int] = field(default_factory=list)
    still: list[int] = field(default_factory=list)
    aux: bytes = b""

    @property
    def full_layout(self) -> bool:
        """Đủ 9 gate chuyển động và 9 gate tĩnh: điều kiện để dùng cho hợp đồng 18 kênh."""
        return self.engineering and self.max_moving_gate == GATES - 1 and self.max_still_gate == GATES - 1

    @property
    def light(self) -> int:
        return self.aux[0] if len(self.aux) >= 1 else -1

    @property
    def out_pin(self) -> int:
        return self.aux[1] if len(self.aux) >= 2 else -1


@dataclass
class Ack:
    command: int
    status: int
    data: bytes


@dataclass
class ParserStats:
    bytes: int = 0
    reports: int = 0
    acks: int = 0
    bad_length: int = 0
    bad_footer: int = 0
    bad_payload: int = 0
    discarded: int = 0  # byte bị bỏ khi dò header


def parse_report_payload(p: bytes) -> Report | None:
    """Kiểm tra và giải mã payload của khung báo cáo. None nếu sai cấu trúc."""
    n = len(p)
    if n < 13 or p[0] not in (TYPE_ENGINEERING, TYPE_BASIC) or p[1] != 0xAA:
        return None
    if p[n - 2] != 0x55 or p[n - 1] != 0x00:
        return None
    r = Report(
        type=p[0],
        target_state=p[2],
        moving_cm=p[3] | (p[4] << 8),
        moving_energy=p[5],
        still_cm=p[6] | (p[7] << 8),
        still_energy=p[8],
        detect_cm=p[9] | (p[10] << 8),
    )
    if p[0] == TYPE_ENGINEERING:
        if n < 13 + 2 + 2:
            return None
        nm, ns = p[11], p[12]
        if nm > GATES - 1 or ns > GATES - 1:
            return None
        end = 13 + (nm + 1) + (ns + 1)
        if end > n - 2:
            return None
        r.engineering = True
        r.max_moving_gate = nm
        r.max_still_gate = ns
        r.moving = list(p[13: 13 + nm + 1])
        r.still = list(p[13 + nm + 1: end])
        r.aux = bytes(p[end: n - 2])
    elif n != 13:
        return None
    return r


class Parser:
    """Bộ phân tích theo luồng: nhận từng đoạn byte bất kỳ, trả về danh sách khung hợp lệ."""

    def __init__(self) -> None:
        self.buf = bytearray()
        self.stats = ParserStats()

    def feed(self, data: bytes) -> list[Report | Ack]:
        out: list[Report | Ack] = []
        self.stats.bytes += len(data)
        for b in data:
            self._push(b, out)
        return out

    def _push(self, b: int, out: list) -> None:
        pending = [b]
        while pending:
            byte = pending.pop(0)
            buf = self.buf
            if not buf:
                if byte in (REPORT_HEADER[0], CMD_HEADER[0]):
                    buf.append(byte)
                else:
                    self.stats.discarded += 1
                continue
            header = REPORT_HEADER if buf[0] == REPORT_HEADER[0] else CMD_HEADER
            if len(buf) < 4:
                if byte == header[len(buf)]:
                    buf.append(byte)
                else:
                    # Không phải header: bỏ byte đầu, dò lại từ các byte còn lại.
                    rest = list(buf[1:]) + [byte]
                    self.stats.discarded += 1
                    buf.clear()
                    pending = rest + pending
                continue
            buf.append(byte)
            if len(buf) == 6:
                length = buf[4] | (buf[5] << 8)
                if length > MAX_PAYLOAD:
                    self.stats.bad_length += 1
                    rest = list(buf[1:])
                    buf.clear()
                    pending = rest + pending
                continue
            if len(buf) < 6:
                continue
            length = buf[4] | (buf[5] << 8)
            total = 4 + 2 + length + 4
            if len(buf) < total:
                continue
            footer = REPORT_FOOTER if header == REPORT_HEADER else CMD_FOOTER
            frame = bytes(buf)
            payload = frame[6: 6 + length]
            ok = False
            if frame[total - 4: total] != footer:
                self.stats.bad_footer += 1
            elif header == REPORT_HEADER:
                rep = parse_report_payload(payload)
                if rep is None:
                    self.stats.bad_payload += 1
                else:
                    self.stats.reports += 1
                    out.append(rep)
                    ok = True
            else:
                if length >= 4:
                    self.stats.acks += 1
                    out.append(
                        Ack(
                            command=payload[0] | (payload[1] << 8),
                            status=payload[2] | (payload[3] << 8),
                            data=bytes(payload[4:]),
                        )
                    )
                    ok = True
                else:
                    self.stats.bad_payload += 1
            buf.clear()
            if not ok:
                # Khung hỏng: một khung thật có thể bắt đầu bên trong nó.
                pending = list(frame[1:]) + pending
                self.stats.discarded += 1


# --------------------------------------------------------------------------------------------
# Tạo khung (cho mô phỏng, phát lại và kiểm thử)
# --------------------------------------------------------------------------------------------

def build_report(
    moving: list[int] | None,
    still: list[int] | None,
    target_state: int = 0,
    moving_cm: int = 0,
    still_cm: int = 0,
    detect_cm: int = 0,
    moving_energy: int | None = None,
    still_energy: int | None = None,
    aux: bytes = bytes([0x7F, 0x00]),
) -> bytes:
    """Khung báo cáo. moving/still = None => khung cơ bản (không Engineering Mode)."""
    mv_e = moving_energy if moving_energy is not None else (max(moving) if moving else 0)
    st_e = still_energy if still_energy is not None else (max(still) if still else 0)
    engineering = moving is not None and still is not None
    p = bytearray(
        [
            TYPE_ENGINEERING if engineering else TYPE_BASIC,
            0xAA,
            target_state & 0xFF,
            moving_cm & 0xFF,
            (moving_cm >> 8) & 0xFF,
            mv_e & 0xFF,
            still_cm & 0xFF,
            (still_cm >> 8) & 0xFF,
            st_e & 0xFF,
            detect_cm & 0xFF,
            (detect_cm >> 8) & 0xFF,
        ]
    )
    if engineering:
        p += bytes([len(moving) - 1, len(still) - 1])
        p += bytes(v & 0xFF for v in moving)
        p += bytes(v & 0xFF for v in still)
        p += aux
    p += bytes([0x55, 0x00])
    return REPORT_HEADER + bytes([len(p) & 0xFF, len(p) >> 8]) + bytes(p) + REPORT_FOOTER


def build_command(command: int, value: bytes = b"") -> bytes:
    body = bytes([command & 0xFF, command >> 8]) + value
    return CMD_HEADER + bytes([len(body) & 0xFF, len(body) >> 8]) + body + CMD_FOOTER


def build_ack(command: int, status: int = 0, value: bytes = b"") -> bytes:
    c = command | ACK_FLAG
    body = bytes([c & 0xFF, c >> 8, status & 0xFF, status >> 8]) + value
    return CMD_HEADER + bytes([len(body) & 0xFF, len(body) >> 8]) + body + CMD_FOOTER


def target_state_from_energies(moving: list[int], still: list[int], thr_m=FACTORY_MOVING, thr_s=FACTORY_STILL) -> int:
    """Trạng thái mục tiêu kiểu radar (dùng khi mô phỏng): bit0 chuyển động, bit1 tĩnh."""
    mv = any(m > t for m, t in zip(moving, thr_m))
    st = any(s > t for g, (s, t) in enumerate(zip(still, thr_s)) if g >= 2)
    return (1 if mv else 0) | (2 if st else 0)
