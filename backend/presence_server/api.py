"""API HTTP (FastAPI). Dashboard dùng /api/*, thiết bị chỉ dùng /api/device/* (token thiết bị)."""

from __future__ import annotations

import hmac
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field

from presence_ml import contract as C

from .service import KEEP, ServiceError

router = APIRouter(prefix="/api")


def svc(request: Request):
    return request.app.state.service


def jobs(request: Request):
    return request.app.state.jobs


def require_admin(request: Request, authorization: str | None = Header(default=None),
                  x_admin_token: str | None = Header(default=None)):
    token = request.app.state.settings.admin_token
    if not token:
        return  # LAN thử nghiệm: không đặt ADMIN_TOKEN (xem docs/security)
    given = x_admin_token or (authorization[7:] if authorization and authorization.startswith("Bearer ") else "")
    if not hmac.compare_digest(given or "", token):
        raise HTTPException(401, "cần ADMIN_TOKEN")


def require_device(request: Request, authorization: str | None = Header(default=None)):
    tokens = request.app.state.settings.device_tokens
    if not tokens:
        return
    given = authorization[7:] if authorization and authorization.startswith("Bearer ") else ""
    if not any(hmac.compare_digest(given, t) for t in tokens):
        raise HTTPException(401, "token thiết bị không hợp lệ")


def call(fn, *args, **kw):
    try:
        return fn(*args, **kw)
    except ServiceError as e:
        raise HTTPException(e.status, str(e)) from e


admin = [Depends(require_admin)]


# ---------------------------------------------------------------- bodies
class DevicePatch(BaseModel):
    name: str | None = None
    room_id: str | None = None


class Presence(BaseModel):
    enter: float
    exit: float
    confirm_occupied_ms: int
    confirm_empty_ms: int


class DeviceConfig(BaseModel):
    presence_model: Presence | None = None
    presence_threshold: Presence | None = None
    prefer_threshold: bool | None = None
    reset_model_params: bool = False


class Command(BaseModel):
    cmd: str


class Room(BaseModel):
    room_id: str = Field(pattern=r"^[A-Za-z0-9_\-]{1,32}$")
    name: str


class SessionStart(BaseModel):
    device_id: str
    room_id: str | None = None
    notes: str = ""


class SessionPatch(BaseModel):
    notes: str | None = None
    split: str | None = None
    room_id: str | None = None


class LabelNow(BaseModel):
    label: str
    note: str = ""


class LabelRange(BaseModel):
    start_ms: int
    end_ms: int
    label: str
    note: str = ""


class EventIn(BaseModel):
    device_id: str
    kind: str
    ts_ms: int | None = None
    note: str = ""


class BaselineStart(BaseModel):
    device_id: str
    name: str
    condition: str = ""
    disturbance: bool = False
    duration_s: float = 120
    confirm_empty: bool = False


class BaselineActivate(BaseModel):
    active: bool = True
    acknowledge_warnings: bool = False


class JobIn(BaseModel):
    include_simulated: bool = False
    overrides: dict = Field(default_factory=dict)


class Approve(BaseModel):
    note: str = ""
    acknowledge_insufficient: bool = False


class ReleaseIn(BaseModel):
    model_id: str
    device_id: str


class ImportIn(BaseModel):
    path: str


# ---------------------------------------------------------------- general
@router.get("/health")
def health(request: Request):
    b = request.app.state.bridge
    return {"ok": True, "mqtt_enabled": b is not None, "mqtt_connected": bool(b and b.connected.is_set()),
            "admin_auth": bool(request.app.state.settings.admin_token),
            "device_auth": bool(request.app.state.settings.device_tokens)}


@router.get("/contract", dependencies=admin)
def contract():
    return {**C.contract_summary(), "class_labels_vi": C.CLASS_LABELS_VI}


# ---------------------------------------------------------------- devices
@router.get("/devices", dependencies=admin)
def devices(request: Request):
    s = svc(request)
    return [s.device_view(d) for d in s.db.all("SELECT * FROM devices ORDER BY device_id")]


@router.get("/devices/{device_id}", dependencies=admin)
def device(device_id: str, request: Request):
    s = svc(request)
    d = s.db.one("SELECT * FROM devices WHERE device_id=?", (device_id,))
    if not d:
        raise HTTPException(404, "không có thiết bị")
    return s.device_view(d)


@router.patch("/devices/{device_id}", dependencies=admin)
def patch_device(device_id: str, body: DevicePatch, request: Request):
    return call(svc(request).set_device, device_id, body.name, body.room_id)


@router.get("/devices/{device_id}/live", dependencies=admin)
def live(device_id: str, request: Request, cursor: int = 0, limit: int = 1500):
    rows = svc(request).live.since(device_id, cursor, limit)
    return {"cursor": rows[-1]["_n"] if rows else cursor, "rows": rows}


@router.put("/devices/{device_id}/config", dependencies=admin)
def device_config(device_id: str, body: DeviceConfig, request: Request):
    patch = {}
    if body.presence_model is not None or body.reset_model_params:
        patch["presence_model"] = None if body.reset_model_params else body.presence_model.model_dump()
    if body.presence_threshold is not None:
        patch["presence_threshold"] = body.presence_threshold.model_dump()
    if body.prefer_threshold is not None:
        patch["prefer_threshold"] = body.prefer_threshold
    return call(svc(request).update_device_config, device_id, patch)


@router.post("/devices/{device_id}/command", dependencies=admin)
def command(device_id: str, body: Command, request: Request):
    return call(svc(request).send_command, device_id, body.cmd)


@router.post("/devices/{device_id}/rollback", dependencies=admin)
def rollback(device_id: str, request: Request):
    return call(svc(request).rollback, device_id)


@router.get("/rooms", dependencies=admin)
def rooms(request: Request):
    return svc(request).db.all("SELECT * FROM rooms ORDER BY room_id")


@router.post("/rooms", dependencies=admin)
def add_room(body: Room, request: Request):
    svc(request).ensure_room(body.room_id, body.name)
    svc(request).db.execute("UPDATE rooms SET name=? WHERE room_id=?", (body.name, body.room_id))
    return {"ok": True}


# ---------------------------------------------------------------- sessions and labels
@router.get("/sessions", dependencies=admin)
def sessions(request: Request, device_id: str | None = None):
    return svc(request).list_sessions(device_id)


@router.post("/sessions", dependencies=admin)
def start_session(body: SessionStart, request: Request):
    return call(svc(request).start_session, body.device_id, "collect", body.room_id, body.notes)


@router.get("/sessions/{session_id}", dependencies=admin)
def get_session(session_id: str, request: Request):
    return call(svc(request).session_view, session_id)


@router.patch("/sessions/{session_id}", dependencies=admin)
def patch_session(session_id: str, body: SessionPatch, request: Request):
    split = body.split if "split" in body.model_fields_set else KEEP
    return call(svc(request).update_session, session_id, body.notes, split, body.room_id)


@router.post("/sessions/{session_id}/stop", dependencies=admin)
def stop_session(session_id: str, request: Request):
    return call(svc(request).stop_session, session_id)


@router.delete("/sessions/{session_id}", dependencies=admin)
def delete_session(session_id: str, request: Request):
    call(svc(request).delete_session, session_id)
    return {"ok": True}


@router.get("/sessions/{session_id}/samples", dependencies=admin)
def session_samples(session_id: str, request: Request, step: int = 1):
    return svc(request).session_samples(session_id, max(1, step))


@router.post("/sessions/{session_id}/label-now", dependencies=admin)
def label_now(session_id: str, body: LabelNow, request: Request):
    return call(svc(request).label_now, session_id, body.label, body.note)


@router.post("/sessions/{session_id}/labels", dependencies=admin)
def set_label(session_id: str, body: LabelRange, request: Request):
    return call(svc(request).set_label, session_id, body.start_ms, body.end_ms, body.label, body.note)


@router.delete("/labels/{label_id}", dependencies=admin)
def delete_label(label_id: int, request: Request):
    svc(request).delete_label(label_id)
    return {"ok": True}


@router.get("/events", dependencies=admin)
def events(request: Request, device_id: str | None = None):
    return svc(request).list_events(device_id)


@router.post("/events", dependencies=admin)
def mark_event(body: EventIn, request: Request):
    return call(svc(request).mark_event, body.device_id, body.kind, body.ts_ms, body.note)


# ---------------------------------------------------------------- baselines
@router.get("/baselines", dependencies=admin)
def baselines(request: Request, device_id: str | None = None):
    return svc(request).list_baselines(device_id)


@router.post("/baselines", dependencies=admin)
def start_baseline(body: BaselineStart, request: Request):
    return call(svc(request).start_baseline, body.device_id, body.name, body.condition, body.disturbance,
                body.duration_s, body.confirm_empty)


@router.post("/baselines/{baseline_id}/finish", dependencies=admin)
def finish_baseline(baseline_id: int, request: Request):
    return call(svc(request).finish_baseline, baseline_id)


@router.post("/baselines/{baseline_id}/activate", dependencies=admin)
def activate_baseline(baseline_id: int, body: BaselineActivate, request: Request):
    return call(svc(request).activate_baseline, baseline_id, body.active, body.acknowledge_warnings)


@router.delete("/baselines/{baseline_id}", dependencies=admin)
def delete_baseline(baseline_id: int, request: Request):
    call(svc(request).delete_baseline, baseline_id)
    return {"ok": True}


# ---------------------------------------------------------------- training jobs
@router.get("/jobs", dependencies=admin)
def list_jobs(request: Request):
    return jobs(request).list()


@router.post("/jobs", dependencies=admin)
def submit_job(body: JobIn, request: Request):
    return call(jobs(request).submit, body.model_dump())


@router.get("/jobs/{job_id}", dependencies=admin)
def get_job(job_id: int, request: Request):
    return call(jobs(request).get, job_id)


@router.get("/jobs/{job_id}/log", dependencies=admin, response_class=PlainTextResponse)
def job_log(job_id: int, request: Request, lines: int = 200):
    return call(jobs(request).log_tail, job_id, lines)


@router.post("/jobs/{job_id}/cancel", dependencies=admin)
def cancel_job(job_id: int, request: Request):
    return call(jobs(request).cancel, job_id)


# ---------------------------------------------------------------- models and releases
@router.get("/models", dependencies=admin)
def models(request: Request):
    return svc(request).list_models()


@router.get("/models/compare", dependencies=admin)
def compare(a: str, b: str, request: Request):
    s = svc(request)
    out = {}
    for mid in (a, b):
        m = call(s.get_model, mid)
        sm = m["summary"]
        out[mid] = {"model_version": m["model_version"], "status": m["status"], "simulated": m["simulated"],
                    "conclusive": m["conclusive"], "insufficient_data": sm.get("insufficient_data"),
                    "evaluation": sm.get("evaluation"), "data": {k: sm.get("data", {}).get(k) for k in
                                                                 ("windows", "split_mode", "split_class_seconds")}}
    return out


@router.get("/models/{model_id}", dependencies=admin)
def get_model(model_id: str, request: Request):
    return call(svc(request).get_model, model_id)


@router.get("/models/{model_id}/report.md", dependencies=admin, response_class=PlainTextResponse)
def model_report(model_id: str, request: Request):
    m = call(svc(request).get_model, model_id)
    p = Path(m["dir"]) / "report.md"
    if not p.exists():
        raise HTTPException(404, "chưa có báo cáo")
    return p.read_text(encoding="utf-8")


@router.post("/models/{model_id}/approve", dependencies=admin)
def approve(model_id: str, body: Approve, request: Request):
    return call(svc(request).approve_model, model_id, body.note, body.acknowledge_insufficient)


@router.post("/models/{model_id}/reject", dependencies=admin)
def reject(model_id: str, body: Approve, request: Request):
    return call(svc(request).reject_model, model_id, body.note)


@router.get("/releases", dependencies=admin)
def releases(request: Request, device_id: str | None = None):
    return svc(request).list_releases(device_id)


@router.post("/releases", dependencies=admin)
def release(body: ReleaseIn, request: Request):
    return call(svc(request).release, body.model_id, body.device_id)


@router.post("/datasets/import", dependencies=admin)
def import_dataset(body: ImportIn, request: Request):
    p = Path(body.path)
    if not p.exists():
        raise HTTPException(404, "không thấy dataset")
    return {"imported": svc(request).import_dataset(p)}


# ---------------------------------------------------------------- device side
@router.get("/device/models/{model_id}/package", dependencies=[Depends(require_device)])
def device_package(model_id: str, request: Request):
    m = call(svc(request).get_model, model_id)
    if m["status"] not in ("approved",):
        raise HTTPException(403, "mô hình chưa được phát hành")
    p = Path(m["dir"]) / "model.pkg"
    if not p.exists():
        raise HTTPException(404, "thiếu gói")
    return FileResponse(p, media_type="application/octet-stream",
                        headers={"X-Package-SHA256": m["package_sha256"], "Cache-Control": "no-store"})
