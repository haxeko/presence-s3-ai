"""Gói mô hình v1 ("PSAIPKG1"): model.tflite + hợp đồng đầu vào + vector kiểm tra + manifest JSON + chữ ký.

Định dạng nhị phân (little-endian) giống ``firmware/main/core/model_package.h``; mô tả đầy đủ trong
``docs/model-package.md``. Firmware chỉ tin khối CONTRACT nhị phân và chữ ký; MANIFEST (JSON) để người và
backend đọc.

Chữ ký: ECDSA P-256 trên SHA-256 của [0, total_size). Khóa riêng nằm ngoài repo (xem tools/keys.py),
khóa công khai được nhúng vào firmware lúc build.
"""

from __future__ import annotations

import hashlib
import json
import struct
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from . import contract as C

MAGIC = b"PSAIPKG1"
HEADER_SIZE = 64
SECTION_ENTRY = 48
MAX_SECTIONS = 8
MODEL_ID_LEN = 32

SECTION_CONTRACT = 1
SECTION_TFLITE = 2
SECTION_TESTVEC = 3
SECTION_MANIFEST = 4
SECTION_NAMES = {SECTION_CONTRACT: "contract", SECTION_TFLITE: "tflite", SECTION_TESTVEC: "testvec",
                 SECTION_MANIFEST: "manifest"}

FLAG_SIMULATED = 1 << 0

SIG_NONE = 0
SIG_ECDSA_P256 = 1

FIRMWARE_MODEL_API = 1  # firmware hiện tại (contract_v1.h kFirmwareModelApi)

TESTVEC_MAGIC = b"PST1"
TESTVEC_HEADER = struct.Struct("<4sIIiiffI")
TESTVEC_RECORD_SIZE = 4 + C.WINDOW_SLOTS * C.N_CHANNELS + 4 * C.N_FEATURES + C.N_FEATURES + C.N_CLASSES + 4

DEFAULT_TOL_INPUT_Q = 1
DEFAULT_TOL_OUTPUT_Q = 3
DEFAULT_TOL_FEATURE_ABS = 1e-4
DEFAULT_TOL_FEATURE_REL = 1e-5


class PackageError(Exception):
    pass


# --------------------------------------------------------------------------------------------
# Vector kiểm tra
# --------------------------------------------------------------------------------------------

@dataclass
class TestVector:
    valid: np.ndarray  # (20,) bool
    energy: np.ndarray  # (20, 18) uint8
    features: np.ndarray  # (72,) float32
    input_q: np.ndarray  # (72,) int8
    output_q: np.ndarray  # (4,) int8
    expected_class: int


@dataclass
class TestVectorSet:
    vectors: list[TestVector]
    tol_input_q: int = DEFAULT_TOL_INPUT_Q
    tol_output_q: int = DEFAULT_TOL_OUTPUT_Q
    tol_feature_abs: float = DEFAULT_TOL_FEATURE_ABS
    tol_feature_rel: float = DEFAULT_TOL_FEATURE_REL

    def pack(self) -> bytes:
        out = bytearray(
            TESTVEC_HEADER.pack(TESTVEC_MAGIC, len(self.vectors), TESTVEC_RECORD_SIZE, self.tol_input_q,
                                self.tol_output_q, self.tol_feature_abs, self.tol_feature_rel, 0)
        )
        for v in self.vectors:
            mask = sum(1 << i for i, ok in enumerate(np.asarray(v.valid, dtype=bool)) if ok)
            rec = (
                struct.pack("<I", mask)
                + np.asarray(v.energy, dtype=np.uint8).reshape(C.WINDOW_SLOTS, C.N_CHANNELS).tobytes()
                + np.asarray(v.features, dtype="<f4").tobytes()
                + np.asarray(v.input_q, dtype=np.int8).tobytes()
                + np.asarray(v.output_q, dtype=np.int8).tobytes()
                + struct.pack("<B3x", int(v.expected_class))
            )
            assert len(rec) == TESTVEC_RECORD_SIZE
            out += rec
        return bytes(out)

    @classmethod
    def unpack(cls, data: bytes) -> "TestVectorSet":
        if len(data) < TESTVEC_HEADER.size:
            raise PackageError("testvec too small")
        magic, count, rsize, ti, to, fa, fr, _ = TESTVEC_HEADER.unpack_from(data, 0)
        if magic != TESTVEC_MAGIC or rsize != TESTVEC_RECORD_SIZE:
            raise PackageError("testvec header")
        if len(data) < TESTVEC_HEADER.size + count * rsize:
            raise PackageError("testvec truncated")
        vecs = []
        for k in range(count):
            off = TESTVEC_HEADER.size + k * rsize
            (mask,) = struct.unpack_from("<I", data, off)
            off += 4
            energy = np.frombuffer(data, dtype=np.uint8, count=C.WINDOW_SLOTS * C.N_CHANNELS, offset=off)
            off += C.WINDOW_SLOTS * C.N_CHANNELS
            feats = np.frombuffer(data, dtype="<f4", count=C.N_FEATURES, offset=off).astype(np.float32)
            off += 4 * C.N_FEATURES
            inq = np.frombuffer(data, dtype=np.int8, count=C.N_FEATURES, offset=off)
            off += C.N_FEATURES
            outq = np.frombuffer(data, dtype=np.int8, count=C.N_CLASSES, offset=off)
            off += C.N_CLASSES
            vecs.append(
                TestVector(
                    valid=np.array([(mask >> i) & 1 == 1 for i in range(C.WINDOW_SLOTS)]),
                    energy=energy.reshape(C.WINDOW_SLOTS, C.N_CHANNELS).copy(),
                    features=feats,
                    input_q=inq.copy(),
                    output_q=outq.copy(),
                    expected_class=data[off],
                )
            )
        return cls(vecs, ti, to, fa, fr)


def make_test_vectors(windows: np.ndarray, valid: np.ndarray, contract: C.ContractBlock, run_int8) -> TestVectorSet:
    """windows (N, 20, 18), valid (N, 20); run_int8(q_in (N, 72) int8) -> q_out (N, 4) int8."""
    feats = C.compute_features_batch(windows, valid)
    z = C.normalize(feats, contract.feature_mean, contract.feature_std)
    q_in = C.quantize_input(z, contract.input_scale, contract.input_zero_point)
    q_out = np.asarray(run_int8(q_in), dtype=np.int8)
    vecs = [
        TestVector(valid=valid[i].astype(bool), energy=windows[i].astype(np.uint8), features=feats[i], input_q=q_in[i],
                   output_q=q_out[i], expected_class=int(np.argmax(q_out[i])))
        for i in range(len(windows))
    ]
    return TestVectorSet(vecs)


# --------------------------------------------------------------------------------------------
# Gói
# --------------------------------------------------------------------------------------------

@dataclass
class PackageInfo:
    model_id: str
    model_version: int
    fw_api_min: int
    fw_api_max: int
    flags: int
    total_size: int
    sections: dict[int, tuple[int, int, bytes]]  # type -> (offset, length, sha256)
    sig_alg: int
    signature: bytes
    sha256: str  # của cả file
    payload: dict[int, bytes] = field(default_factory=dict)

    @property
    def simulated(self) -> bool:
        return bool(self.flags & FLAG_SIMULATED)

    def manifest(self) -> dict:
        return json.loads(self.payload[SECTION_MANIFEST].decode()) if SECTION_MANIFEST in self.payload else {}

    def contract(self) -> C.ContractBlock:
        return C.ContractBlock.unpack(self.payload[SECTION_CONTRACT])


def _align16(n: int) -> int:
    return (n + 15) & ~15


def build_package(
    model_id: str,
    model_version: int,
    contract_bin: bytes,
    tflite: bytes,
    testvec: bytes,
    manifest: dict,
    fw_api: tuple[int, int] = (FIRMWARE_MODEL_API, FIRMWARE_MODEL_API),
    flags: int = 0,
    private_key_pem: bytes | None = None,
) -> bytes:
    mid = model_id.encode()
    if not mid or len(mid) > MODEL_ID_LEN:
        raise PackageError("model_id must be 1..32 bytes")
    if len(contract_bin) != C.CONTRACT_BIN_SIZE:
        raise PackageError("contract block size")
    sections = [(SECTION_CONTRACT, contract_bin), (SECTION_TFLITE, tflite), (SECTION_TESTVEC, testvec)]
    manifest = dict(manifest)
    manifest.setdefault("sections", {})
    for t, data in sections:
        manifest["sections"][SECTION_NAMES[t]] = {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    manifest_bytes = json.dumps(manifest, ensure_ascii=False, indent=1, sort_keys=True).encode()
    sections.append((SECTION_MANIFEST, manifest_bytes))

    table_end = HEADER_SIZE + SECTION_ENTRY * len(sections)
    offset = _align16(table_end)
    layout = []
    for t, data in sections:
        layout.append((t, offset, data))
        offset = _align16(offset + len(data))
    total = offset
    buf = bytearray(total)
    struct.pack_into("<8sIIII2H32sI", buf, 0, MAGIC, HEADER_SIZE, len(sections), total, model_version,
                     fw_api[0], fw_api[1], mid.ljust(MODEL_ID_LEN, b"\0"), flags)
    for i, (t, off, data) in enumerate(layout):
        struct.pack_into("<IIII32s", buf, HEADER_SIZE + SECTION_ENTRY * i, t, off, len(data), 0,
                         hashlib.sha256(data).digest())
        buf[off: off + len(data)] = data
    digest = hashlib.sha256(bytes(buf)).digest()
    if private_key_pem:
        sig = sign_digest(private_key_pem, digest)
        trailer = struct.pack("<4sHH", b"PSG1", SIG_ECDSA_P256, len(sig)) + sig
    else:
        trailer = struct.pack("<4sHH", b"PSG1", SIG_NONE, 0)
    return bytes(buf) + trailer


def parse_package(data: bytes) -> PackageInfo:
    if len(data) < HEADER_SIZE + 8:
        raise PackageError("package_too_small")
    magic, hsize, count, total, version, api_min, api_max, mid, flags = struct.unpack_from("<8sIIII2H32sI", data, 0)
    if magic != MAGIC:
        raise PackageError("package_magic")
    if hsize != HEADER_SIZE or not (1 <= count <= MAX_SECTIONS):
        raise PackageError("package_header")
    table_end = HEADER_SIZE + SECTION_ENTRY * count
    if total < table_end or total > len(data):
        raise PackageError("package_header")
    sections: dict[int, tuple[int, int, bytes]] = {}
    spans = []
    for i in range(count):
        t, off, length, _, sha = struct.unpack_from("<IIII32s", data, HEADER_SIZE + SECTION_ENTRY * i)
        if off < table_end or off % 16 or length > total or off > total - length or t in sections:
            raise PackageError("package_sections")
        for o_off, o_len in spans:
            if off < o_off + o_len and o_off < off + length:
                raise PackageError("package_sections")
        spans.append((off, length))
        sections[t] = (off, length, sha)
    for req in (SECTION_CONTRACT, SECTION_TFLITE, SECTION_TESTVEC):
        if req not in sections:
            raise PackageError("package_sections")
    if len(data) < total + 8 or data[total: total + 4] != b"PSG1":
        raise PackageError("signature")
    alg, sig_len = struct.unpack_from("<HH", data, total + 4)
    if len(data) < total + 8 + sig_len or (alg == SIG_NONE and sig_len) or alg not in (SIG_NONE, SIG_ECDSA_P256):
        raise PackageError("signature")
    payload = {t: bytes(data[o: o + n]) for t, (o, n, _) in sections.items()}
    return PackageInfo(
        model_id=mid.rstrip(b"\0").decode(),
        model_version=version,
        fw_api_min=api_min,
        fw_api_max=api_max,
        flags=flags,
        total_size=total,
        sections=sections,
        sig_alg=alg,
        signature=bytes(data[total + 8: total + 8 + sig_len]),
        sha256=hashlib.sha256(data).hexdigest(),
        payload=payload,
    )


def verify_package(
    data: bytes,
    public_key_pem: bytes | None = None,
    expected_sha256: str | None = None,
    firmware_model_api: int = FIRMWARE_MODEL_API,
    radar_gate_cm: int = 0,
) -> PackageInfo:
    """Cùng các kiểm tra firmware làm trước khi chạy thử (trừ suy luận)."""
    if expected_sha256 and hashlib.sha256(data).hexdigest() != expected_sha256.lower():
        raise PackageError("sha256")
    info = parse_package(data)
    if not (info.fw_api_min <= firmware_model_api <= info.fw_api_max):
        raise PackageError("firmware_incompatible")
    for t, (off, length, sha) in info.sections.items():
        if hashlib.sha256(data[off: off + length]).digest() != sha:
            raise PackageError("section_hash")
    if public_key_pem is not None:
        if info.sig_alg != SIG_ECDSA_P256 or not info.signature:
            raise PackageError("signature")
        digest = hashlib.sha256(data[: info.total_size]).digest()
        if not verify_digest(public_key_pem, digest, info.signature):
            raise PackageError("signature")
    c = info.contract()
    c.check_supported()
    if radar_gate_cm and c.gate_resolution_cm != radar_gate_cm:
        raise PackageError("radar_resolution")
    return info


# --------------------------------------------------------------------------------------------
# Khóa ký (cryptography)
# --------------------------------------------------------------------------------------------

def generate_keypair() -> tuple[bytes, bytes]:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    priv = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())
    pub = key.public_key().public_bytes(serialization.Encoding.PEM,
                                        serialization.PublicFormat.SubjectPublicKeyInfo)
    return priv, pub


def public_from_private(private_key_pem: bytes) -> bytes:
    from cryptography.hazmat.primitives import serialization

    key = serialization.load_pem_private_key(private_key_pem, password=None)
    return key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)


def sign_digest(private_key_pem: bytes, digest: bytes) -> bytes:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec, utils

    key = serialization.load_pem_private_key(private_key_pem, password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey) or key.curve.name != "secp256r1":
        raise PackageError("signing key must be ECDSA P-256")
    return key.sign(digest, ec.ECDSA(utils.Prehashed(hashes.SHA256())))


def verify_digest(public_key_pem: bytes, digest: bytes, signature: bytes) -> bool:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec, utils

    key = serialization.load_pem_public_key(public_key_pem)
    try:
        key.verify(signature, digest, ec.ECDSA(utils.Prehashed(hashes.SHA256())))
        return True
    except InvalidSignature:
        return False


def load_key(path: str | Path | None) -> bytes | None:
    if not path:
        return None
    p = Path(path)
    return p.read_bytes() if p.exists() else None
