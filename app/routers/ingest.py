from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from typing import List, Optional, Any
import hashlib
import time

from app.auth.apikey import require_ingest_key
from app.services.firestore import db, resolve_hardware, readings_col, sensor_ref, acks_col, normalize_hw

router = APIRouter()

class TelemetryPayload(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str
    fw: Optional[int] = None
    cv: Optional[int] = None
    cc: Optional[str] = None
    b: Optional[float] = None
    s: Optional[float] = None
    la: Optional[float] = None
    lo: Optional[float] = None
    ga: Optional[int] = None
    f: Optional[List[str]] = None
    t: List[Optional[int]]
    d: List[List[Optional[float]]]

class AckPayload(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str
    ok: Optional[int] = None
    m: Optional[str] = None
    av: Optional[int] = None
    ac: Optional[str] = None
    nv: Optional[int] = None
    nc: Optional[str] = None

def compute_bucket_start(t: List[Optional[int]]) -> Optional[int]:
    ints = [x for x in t if isinstance(x, int)]
    if len(ints) != len(t) or len(ints) < 2:
        return None
    step = ints[1] - ints[0]
    if step <= 0 or step > 86400:
        return None
    period = step * len(ints)
    last = ints[-1]
    return last - (last % period)

@router.post("/sensors/telemetry/{hardwareId}")
def ingest_telemetry(hardwareId: str, payload: TelemetryPayload, _: None = Depends(require_ingest_key)):
    hw = normalize_hw(hardwareId)
    if normalize_hw(payload.id) != hw:
        raise HTTPException(status_code=400, detail="hardwareId mismatch (URL vs payload.id)")

    try:
        tenantId, sensorId = resolve_hardware(hw)
    except KeyError:
        raise HTTPException(status_code=404, detail="hardwareId not registered")

    bucket = compute_bucket_start(payload.t)
    if bucket is None:
        now = int(time.time())
        bucket = now - (now % 300)

    raw = payload.model_dump_json()
    h = hashlib.sha256(raw.encode("utf-8")).hexdigest()

    base_id = str(bucket)
    doc_ref = readings_col(tenantId, sensorId).document(base_id)
    snap = doc_ref.get()
    if snap.exists:
        old = snap.to_dict() or {}
        if old.get("hash") == h:
            return {"ok": True, "deduped": True, "readingId": base_id}
        # Store under a derived id
        doc_ref = readings_col(tenantId, sensorId).document(f"{base_id}-{h[:8]}")

    doc_ref.set({
        "receivedAt": db.SERVER_TIMESTAMP,
        "readingId": doc_ref.id,
        "bucketStart": bucket,
        "lastTs": payload.t[-1] if payload.t else None,
        "cv": payload.cv,
        "cc": payload.cc,
        "hash": h,
        "payloadRaw": raw,
    })

    # Update sensor status
    sref = sensor_ref(tenantId, sensorId)
    sref.set({
        "status": {
            "lastSeenAt": db.SERVER_TIMESTAMP,
            "batteryPct": payload.b,
            "signalDbm": payload.s,
            "lastGps": {"la": payload.la, "lo": payload.lo, "ga": payload.ga} if payload.la is not None and payload.lo is not None else None,
        },
        "updatedAt": db.SERVER_TIMESTAMP,
    }, merge=True)

    return {"ok": True, "readingId": doc_ref.id}

@router.post("/sensors/ack/{hardwareId}")
def ingest_ack(hardwareId: str, payload: AckPayload, _: None = Depends(require_ingest_key)):
    hw = normalize_hw(hardwareId)
    if normalize_hw(payload.id) != hw:
        raise HTTPException(status_code=400, detail="hardwareId mismatch (URL vs payload.id)")

    try:
        tenantId, sensorId = resolve_hardware(hw)
    except KeyError:
        raise HTTPException(status_code=404, detail="hardwareId not registered")

    raw = payload.model_dump_json()
    h = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    now = int(time.time())
    ack_id = f"{now}-{h[:8]}"

    acks_col(tenantId, sensorId).document(ack_id).set({
        "receivedAt": db.SERVER_TIMESTAMP,
        "hash": h,
        "payloadRaw": raw,
        "nv": payload.nv,
        "ok": payload.ok,
        "m": payload.m,
    })

    # Update sensor status
    sensor_ref(tenantId, sensorId).set({
        "status": {"lastAckAt": db.SERVER_TIMESTAMP, "lastAckOk": payload.ok, "lastAckMsg": payload.m},
        "updatedAt": db.SERVER_TIMESTAMP,
    }, merge=True)

    return {"ok": True, "ackId": ack_id}
