from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Any, Dict, Optional
import re

from app.auth.firebase import verify_bearer
from app.auth.tenant_authz import authorize_tenant
from app.services.firestore import db, sensor_ref, configs_col, hardware_index_ref, generate_hw_id, normalize_hw
from app.services.plan_schema import validate_plan
from app.services.plan_codec import canonical_json_bytes, crc32_hex
from app.services.mqtt_pub import publish_retained

router = APIRouter()

HEX6 = re.compile(r"^[0-9A-F]{6}$")

class CreateSensorReq(BaseModel):
    name: str
    fieldId: Optional[str] = None
    location: Optional[Dict[str, float]] = None

@router.post("/tenants/{tenantId}/sensors")
def create_sensor(tenantId: str, req: CreateSensorReq, user=Depends(verify_bearer)):
    authorize_tenant(tenantId, user, min_role="tech")

    sens_ref = db.collection("tenants").document(tenantId).collection("sensors").document()
    sensorId = sens_ref.id

    for _ in range(20):
        hw = generate_hw_id()
        if not HEX6.match(hw):
            continue
        idx_ref = hardware_index_ref(hw)

        def txn_op(txn):
            # Claim HW ID globally (fails if exists)
            txn.create(idx_ref, {"tenantId": tenantId, "sensorId": sensorId, "createdAt": db.SERVER_TIMESTAMP})
            # Create sensor
            txn.set(sens_ref, {
                "name": req.name,
                "fieldId": req.fieldId,
                "location": req.location,
                "hardwareId": hw,
                "status": {},
                "activeConfig": {"ver": 0, "cc": None, "updatedAt": db.SERVER_TIMESTAMP},
                "createdAt": db.SERVER_TIMESTAMP,
                "updatedAt": db.SERVER_TIMESTAMP,
            })

        try:
            db.transaction()(txn_op)
            return {"sensorId": sensorId, "hardwareId": hw}
        except Exception:
            continue

    raise HTTPException(status_code=500, detail="Failed to allocate hardwareId")

@router.post("/tenants/{tenantId}/sensors/{sensorId}/configs:publish")
def publish_config(tenantId: str, sensorId: str, plan: Dict[str, Any], user=Depends(verify_bearer)):
    authorize_tenant(tenantId, user, min_role="tech")

    try:
        validate_plan(plan)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid plan: {e}")

    sref = sensor_ref(tenantId, sensorId)
    snap = sref.get()
    if not snap.exists:
        raise HTTPException(status_code=404, detail="sensor not found")
    sdata = snap.to_dict() or {}
    hw = normalize_hw(sdata.get("hardwareId", ""))
    if not HEX6.match(hw):
        raise HTTPException(status_code=500, detail="sensor hardwareId invalid")

    plan_bytes = canonical_json_bytes(plan)
    cc = crc32_hex(plan_bytes)
    plan_str = plan_bytes.decode("utf-8")

    def txn_op(txn):
        s = txn.get(sref)
        cur = (s.to_dict() or {}).get("activeConfig", {}) if s.exists else {}
        cur_ver = int(cur.get("ver") or 0)
        new_ver = cur_ver + 1

        cfg_ref = configs_col(tenantId, sensorId).document(str(new_ver))
        txn.set(cfg_ref, {
            "ver": new_ver,
            "cc": cc,
            "json": plan_str,
            "createdAt": db.SERVER_TIMESTAMP,
            "createdByUid": user.get("uid"),
            "publishedAt": db.SERVER_TIMESTAMP,
        })
        txn.set(sref, {
            "activeConfig": {"ver": new_ver, "cc": cc, "updatedAt": db.SERVER_TIMESTAMP},
            "updatedAt": db.SERVER_TIMESTAMP,
        }, merge=True)
        return new_ver

    try:
        new_ver = db.transaction()(txn_op)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"firestore txn fail: {e}")

    cfg_topic = f"/sensors/config/{hw}"
    meta_topic = f"/sensors/config-meta/{hw}"

    publish_retained(cfg_topic, plan_str, qos=1)
    publish_retained(meta_topic, f'{{"ver":{new_ver},"cc":"{cc}"}}', qos=1)

    return {"ver": new_ver, "cc": cc, "topics": {"config": cfg_topic, "meta": meta_topic}}

@router.post("/tenants/{tenantId}/sensors/{sensorId}/configs/{ver}:republish")
def republish_config(tenantId: str, sensorId: str, ver: int, user=Depends(verify_bearer)):
    authorize_tenant(tenantId, user, min_role="tech")

    sref = sensor_ref(tenantId, sensorId)
    snap = sref.get()
    if not snap.exists:
        raise HTTPException(status_code=404, detail="sensor not found")
    sdata = snap.to_dict() or {}
    hw = normalize_hw(sdata.get("hardwareId", ""))
    if not HEX6.match(hw):
        raise HTTPException(status_code=500, detail="sensor hardwareId invalid")

    cfg_ref = configs_col(tenantId, sensorId).document(str(ver))
    cfg_snap = cfg_ref.get()
    if not cfg_snap.exists:
        raise HTTPException(status_code=404, detail="config not found")
    cfg = cfg_snap.to_dict() or {}
    plan_str = cfg.get("json")
    cc = cfg.get("cc")
    if not isinstance(plan_str, str) or not isinstance(cc, str):
        raise HTTPException(status_code=500, detail="stored config invalid")

    cfg_topic = f"/sensors/config/{hw}"
    meta_topic = f"/sensors/config-meta/{hw}"

    publish_retained(cfg_topic, plan_str, qos=1)
    publish_retained(meta_topic, f'{{"ver":{ver},"cc":"{cc}"}}', qos=1)

    cfg_ref.set({"republishedAt": db.SERVER_TIMESTAMP, "republishedByUid": user.get("uid")}, merge=True)

    return {"ok": True, "ver": ver, "cc": cc, "topics": {"config": cfg_topic, "meta": meta_topic}}
