from fastapi import FastAPI, HTTPException, Depends, Query, Header
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any, Tuple
from datetime import datetime, timezone, timedelta
import os

import firebase_admin
from firebase_admin import credentials, firestore


# ---------- CONFIG ----------
API_KEY = os.getenv("AGROMIND_API_KEY", "iC6919i3f88i342Q")

SAMPLE_INTERVAL_SEC_DEFAULT = 900
SAMPLES_PER_BATCH_DEFAULT = 4
RAW_RETENTION_DAYS = int(os.getenv("RAW_RETENTION_DAYS", "60"))

MAX_SCHEDULE = 4
MAX_ITEMS_PER_BATCH = 4
MAX_SAMPLES_PER_ITEM = 48

RANGE_MAP = {
    "1h": timedelta(hours=1),
    "6h": timedelta(hours=6),
    "12h": timedelta(hours=12),
    "1d": timedelta(days=1),
    "1w": timedelta(weeks=1),
    "1m": timedelta(days=30),
    "3m": timedelta(days=90),
    "6m": timedelta(days=180),
    "1y": timedelta(days=365),
}


# ---------- FIREBASE ----------
cred = credentials.Certificate("firebase-account.json")
firebase_admin.initialize_app(cred)
db = firestore.client()


# ---------- FASTAPI ----------
app = FastAPI(title="AgroMind Telemetry API")


@app.get("/")
def root():
    return {"status": "ok", "message": "AgroMind Telemetry API"}


# ---------- AUTH ----------
def verify_api_key(
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
    k: Optional[str] = Query(default=None)
):
    key = x_api_key or k
    if key != API_KEY:
        raise HTTPException(status_code=401, detail="API key inválida")


# ---------- HELPERS ----------
def get_time_window(range_key: str):
    if range_key not in RANGE_MAP:
        raise HTTPException(status_code=400, detail="Rango de tiempo no válido")
    now = datetime.now(timezone.utc)
    start = now - RANGE_MAP[range_key]
    return start, now


def day_id(dt: datetime) -> str:
    return dt.strftime("%Y%m%d")


def day_start_utc(dt: datetime) -> datetime:
    return datetime(dt.year, dt.month, dt.day, tzinfo=timezone.utc)


# ---------- MODELOS (CONFIG) ----------
class ScheduleItem(BaseModel):
    sensorId: int = Field(..., description="Firmware sensorId (int), usado por el schedule del dispositivo")
    rail: int = Field(..., description="0..2 (o -1 si no quieres toggle)")
    warmupMs: int = Field(0, description="Warmup extra por lectura")


class TelemetryConfigOut(BaseModel):
    intervalSec: int = SAMPLE_INTERVAL_SEC_DEFAULT
    samplesPerBatch: int = SAMPLES_PER_BATCH_DEFAULT
    schedule: List[ScheduleItem] = Field(default_factory=list)


# ---------- MODELOS (INGEST COMPACT) ----------
class CompactBatchItem(BaseModel):
    t: int = Field(..., description="typeCode int (1..9)")
    s: List[Any] = Field(..., description="samples compactos (números o arrays)")

class CompactBatchTelemetry(BaseModel):
    i: str = Field(..., description="deviceId")
    b: Optional[int] = Field(None, description="battery*10 (0..1000)")
    s: Optional[int] = Field(None, description="signal dBm*10 (negativo)")
    iv: Optional[int] = Field(None, description="intervalSec real del batch")
    la: Optional[int] = Field(None, description="lat * 1e6 (int)")
    lo: Optional[int] = Field(None, description="lon * 1e6 (int)")
    it: List[CompactBatchItem] = Field(..., description="items por sensor (máx 4)")


# ---------- TYPE MAP / PARSER ----------
# IMPORTANTE: estas escalas deben coincidir con lo que envía el firmware:
# - soil moisture: % entero
# - hygrometer: at10, rh10 => /10
# - thermal: tt10 => /10
# - leaf: [wet(0/1), wd(sec), m10] => m10/10
# - ORP: mV entero
# - tension: 0.1kPa => /10
def values_from_compact(type_code: int, sample: Any) -> Dict[str, float]:
    # Type 1: NPK => [n,p,k] enteros
    if type_code == 1:
        if not (isinstance(sample, list) and len(sample) == 3):
            raise HTTPException(400, detail="type=1 (npk) espera [n,p,k]")
        return {
            "nitrogen_mgkg": float(sample[0]),
            "phosphorus_mgkg": float(sample[1]),
            "potassium_mgkg": float(sample[2]),
        }

    # Type 2: soil moisture => v
    if type_code == 2:
        if isinstance(sample, list):
            if len(sample) != 1:
                raise HTTPException(400, detail="type=2 (soil) espera v o [v]")
            sample = sample[0]
        return {"vwc_percent": float(sample)}

    # Type 3: fert => [ecX, stX]
    if type_code == 3:
        if not (isinstance(sample, list) and len(sample) == 2):
            raise HTTPException(400, detail="type=3 (fert) espera [ec,st]")
        return {
            "ec_mscm": float(sample[0]) / 100.0,
            "solution_temp_c": float(sample[1]) / 10.0
        }

    # Type 4: hygro => [at10, rh10]
    if type_code == 4:
        if not (isinstance(sample, list) and len(sample) == 2):
            raise HTTPException(400, detail="type=4 (hygro) espera [at10,rh10]")
        return {
            "air_temp_c": float(sample[0]) / 10.0,
            "rh_percent": float(sample[1]) / 10.0
        }

    # Type 5: leaf => [wet(0/1), wd(sec), m10]
    if type_code == 5:
        if not (isinstance(sample, list) and len(sample) == 3):
            raise HTTPException(400, detail="type=5 (leaf) espera [wet,wd,m10]")
        wet = 1.0 if int(sample[0]) != 0 else 0.0
        return {
            "wet": wet,
            "wet_duration_s": float(sample[1]),
            "leaf_moist_pct": float(sample[2]) / 10.0
        }

    # Type 6: rain => [rX, riX]
    if type_code == 6:
        if not (isinstance(sample, list) and len(sample) == 2):
            raise HTTPException(400, detail="type=6 (rain) espera [r,ri]")
        return {
            "rainfall_mm": float(sample[0]) / 10.0,
            "intensity_mm_h": float(sample[1]) / 10.0
        }

    # Type 7: thermal => tt10
    if type_code == 7:
        if isinstance(sample, list):
            if len(sample) != 1:
                raise HTTPException(400, detail="type=7 (thermal) espera tt o [tt]")
            sample = sample[0]
        return {"temperature_c": float(sample) / 10.0}

    # Type 8: ORP mV entero
    if type_code == 8:
        if isinstance(sample, list):
            if len(sample) != 1:
                raise HTTPException(400, detail="type=8 (orp) espera mv o [mv]")
            sample = sample[0]
        return {"orp_mv": float(sample)}

    # Type 9: Soil tension (0.1kPa) => kPa
    if type_code == 9:
        if isinstance(sample, list):
            if len(sample) != 1:
                raise HTTPException(400, detail="type=9 (tension) espera x o [x]")
            sample = sample[0]
        return {"tension_kpa": float(sample) / 10.0}

    raise HTTPException(status_code=400, detail=f"type no soportado: {type_code}")


# ---------- DEVICE RESOLUTION / SENSOR MAP ----------
def resolve_tenant(device_id: str) -> str:
    idx_ref = db.document(f"deviceIndex/{device_id}")
    idx_snap = idx_ref.get()
    if idx_snap.exists:
        d = idx_snap.to_dict() or {}
        tid = d.get("tenantId")
        if tid:
            return tid

    docs = list(
        db.collection_group("sensors")
          .where("hardwareId", "==", device_id)
          .limit(20)
          .stream()
    )
    if not docs:
        docs = list(
            db.collection_group("sensors")
              .where("deviceId", "==", device_id)
              .limit(20)
              .stream()
        )
    if not docs:
        raise HTTPException(status_code=404, detail=f"Dispositivo no registrado: {device_id}")

    parts = docs[0].reference.path.split("/")
    tenant_id = parts[1]

    idx_ref.set({"tenantId": tenant_id, "updatedAt": firestore.SERVER_TIMESTAMP}, merge=True)
    return tenant_id


def get_device_ref(tenant_id: str, device_id: str):
    return db.document(f"tenants/{tenant_id}/devices/{device_id}")


def get_or_build_sensor_map(tenant_id: str, device_id: str) -> Dict[int, str]:
    device_ref = get_device_ref(tenant_id, device_id)
    device_snap = device_ref.get()
    if device_snap.exists:
        d = device_snap.to_dict() or {}
        sm = d.get("sensorMap")
        if isinstance(sm, dict) and sm:
            out = {}
            for k, v in sm.items():
                try:
                    out[int(k)] = str(v)
                except Exception:
                    continue
            if out:
                return out

    idx_ref = db.document(f"deviceIndex/{device_id}")
    idx_snap = idx_ref.get()
    if idx_snap.exists:
        d = idx_snap.to_dict() or {}
        sm = d.get("sensorMap")
        if isinstance(sm, dict) and sm:
            out = {}
            for k, v in sm.items():
                try:
                    out[int(k)] = str(v)
                except Exception:
                    continue
            if out:
                return out

    sensors = list(
        db.collection(f"tenants/{tenant_id}/sensors")
          .where("hardwareId", "==", device_id)
          .stream()
    )
    if not sensors:
        sensors = list(
            db.collection(f"tenants/{tenant_id}/sensors")
              .where("deviceId", "==", device_id)
              .stream()
        )

    if not sensors:
        raise HTTPException(status_code=404, detail=f"No hay sensores asociados a deviceId={device_id} en tenant={tenant_id}")

    sensor_map: Dict[int, str] = {}
    for s in sensors:
        sd = s.to_dict() or {}
        tc = None
        tel = sd.get("telemetry") or {}
        if isinstance(tel, dict):
            tc = tel.get("typeCode")

        if tc is None:
            tc = sd.get("typeCode")

        if tc is None:
            continue

        try:
            tc_int = int(tc)
        except Exception:
            continue

        if tc_int in sensor_map:
            raise HTTPException(
                status_code=409,
                detail=f"typeCode duplicado {tc_int} en múltiples sensores para deviceId={device_id}"
            )

        sensor_map[tc_int] = s.id

    if not sensor_map:
        raise HTTPException(status_code=400, detail=f"No se pudo construir sensorMap para deviceId={device_id}. Falta telemetry.typeCode en sensores.")

    device_ref.set({"sensorMap": {str(k): v for k, v in sensor_map.items()}, "updatedAt": firestore.SERVER_TIMESTAMP}, merge=True)
    idx_ref.set({"tenantId": tenant_id, "sensorMap": {str(k): v for k, v in sensor_map.items()}, "updatedAt": firestore.SERVER_TIMESTAMP}, merge=True)

    return sensor_map


# ---------- DAILY AGG ----------
@firestore.transactional
def tx_apply_daily_agg(transaction: firestore.Transaction, agg_ref, day_ts: datetime, updates: Dict[str, Any]):
    snap = transaction.get(agg_ref)
    doc = snap.to_dict() if snap.exists else {"day": day_ts, "metrics": {}, "seen": {}}
    doc.setdefault("day", day_ts)
    doc.setdefault("metrics", {})
    doc.setdefault("seen", {})

    seen: Dict[str, bool] = doc["seen"]
    metrics: Dict[str, Dict[str, Any]] = doc["metrics"]

    metrics_by_reading: Dict[str, Dict[str, float]] = updates["_metricsByReading"]

    for reading_id, values in metrics_by_reading.items():
        if seen.get(reading_id):
            continue

        seen[reading_id] = True

        for k, v in values.items():
            cur = metrics.get(k) or {"min": v, "max": v, "sum": 0.0, "count": 0}
            cur["min"] = min(float(cur.get("min", v)), v)
            cur["max"] = max(float(cur.get("max", v)), v)
            cur["sum"] = float(cur.get("sum", 0.0)) + float(v)
            cur["count"] = int(cur.get("count", 0)) + 1
            metrics[k] = cur

    doc["metrics"] = metrics
    doc["seen"] = seen
    doc["updatedAt"] = firestore.SERVER_TIMESTAMP
    transaction.set(agg_ref, doc, merge=True)


# ---------- INGEST (COMPACT BATCH) ----------
def ingest_compact_batch(payload: CompactBatchTelemetry):
    if not payload.it:
        raise HTTPException(status_code=400, detail="it vacío")

    if len(payload.it) > MAX_ITEMS_PER_BATCH:
        raise HTTPException(status_code=400, detail=f"Máximo {MAX_ITEMS_PER_BATCH} items por batch")

    device_id = payload.i
    tenant_id = resolve_tenant(device_id)
    sensor_map = get_or_build_sensor_map(tenant_id, device_id)

    now = datetime.now(timezone.utc)

    interval = int(payload.iv or SAMPLE_INTERVAL_SEC_DEFAULT)
    if interval < 60:
        interval = 60

    battery_pct = (float(payload.b) / 10.0) if payload.b is not None else None
    rssi_dbm = (float(payload.s) / 10.0) if payload.s is not None else None
    lat = (float(payload.la) / 1e6) if payload.la is not None else None
    lon = (float(payload.lo) / 1e6) if payload.lo is not None else None

    meta_base = {
        "deviceId": device_id,
        "intervalSec": interval,
        "batteryPct": battery_pct,
        "rssi": rssi_dbm,
        "lat": lat,
        "lon": lon,
    }

    b = db.batch()

    dev_ref = get_device_ref(tenant_id, device_id)
    dev_status_update = {
        "status.lastSeenAt": now,
        "status.batteryPct": battery_pct,
        "status.rssi": rssi_dbm,
    }
    if lat is not None and lon is not None:
        dev_status_update["status.lastLat"] = lat
        dev_status_update["status.lastLon"] = lon

    b.set(dev_ref, dev_status_update, merge=True)

    daily_by_sensor: Dict[str, Dict[str, Dict[str, Dict[str, float]]]] = {}

    ingested_total = 0
    sensors_touched: List[str] = []

    for item in payload.it:
        type_code = int(item.t)
        samples = item.s
        if not samples:
            continue

        if len(samples) > MAX_SAMPLES_PER_ITEM:
            raise HTTPException(status_code=400, detail=f"Demasiadas muestras en item type={type_code}")

        sensor_doc_id = sensor_map.get(type_code)
        if not sensor_doc_id:
            raise HTTPException(
                status_code=404,
                detail=f"No hay sensor asignado para typeCode={type_code} en deviceId={device_id}. Revisa telemetry.typeCode en Firestore."
            )

        sensor_ref = db.document(f"tenants/{tenant_id}/sensors/{sensor_doc_id}")
        sensors_touched.append(sensor_doc_id)

        n = len(samples)
        last_ts = None
        last_values = None

        for i, smp in enumerate(samples):
            ts = now - timedelta(seconds=(n - 1 - i) * interval)
            values = values_from_compact(type_code, smp)

            last_ts = ts
            last_values = values

            reading_id = ts.strftime("%Y%m%d%H%M")
            reading_ref = db.document(f"tenants/{tenant_id}/sensors/{sensor_doc_id}/readings/{reading_id}")

            expires_at = ts + timedelta(days=RAW_RETENTION_DAYS)

            data: Dict[str, Any] = {
                "ts": ts,
                "updatedAt": firestore.SERVER_TIMESTAMP,
                "meta": meta_base,
                "expiresAt": expires_at,
                "meta.typeCode": type_code,
            }
            for k, v in values.items():
                data[f"values.{k}"] = float(v)

            b.set(reading_ref, data, merge=True)

            did = day_id(ts)
            daily_by_sensor.setdefault(sensor_doc_id, {})
            daily_by_sensor[sensor_doc_id].setdefault(did, {})
            daily_by_sensor[sensor_doc_id][did][reading_id] = values

            ingested_total += 1

        sensor_update: Dict[str, Any] = {
            "status.batteryPct": battery_pct,
            "status.rssi": rssi_dbm,
            "status.lastSeenAt": now,
            "lastReading.ts": last_ts,
            "lastReading.values": last_values,
            "lastReading.typeCode": type_code,
        }
        if lat is not None and lon is not None:
            sensor_update["status.lastLat"] = lat
            sensor_update["status.lastLon"] = lon

        b.set(sensor_ref, sensor_update, merge=True)

    b.commit()

    updated_days = []
    for sensor_doc_id, days_map in daily_by_sensor.items():
        for did, readings_map in days_map.items():
            day_ts = day_start_utc(datetime.strptime(did, "%Y%m%d").replace(tzinfo=timezone.utc))
            agg_ref = db.document(f"tenants/{tenant_id}/sensors/{sensor_doc_id}/dailyAgg/{did}")
            tx = db.transaction()
            tx_apply_daily_agg(tx, agg_ref, day_ts, {"_metricsByReading": readings_map})
            updated_days.append({"sensorDocId": sensor_doc_id, "day": did})

    return {
        "status": "success",
        "tenantId": tenant_id,
        "deviceId": device_id,
        "ingestedReadings": ingested_total,
        "sensorsTouched": list(sorted(set(sensors_touched))),
        "updatedDailyAgg": updated_days[:50],
    }


# ---------- POST: single endpoint ----------
@app.post("/telemetry/batch")
def post_telemetry_batch(data: CompactBatchTelemetry, _: None = Depends(verify_api_key)):
    return ingest_compact_batch(data)


# ---------- GET: resolve ----------
@app.get("/devices/{device_id}/resolve")
def get_device_resolve(device_id: str, _: None = Depends(verify_api_key)):
    tenant_id = resolve_tenant(device_id)
    dev_ref = get_device_ref(tenant_id, device_id)
    dev_snap = dev_ref.get()
    sensor_map = get_or_build_sensor_map(tenant_id, device_id)

    return {
        "deviceId": device_id,
        "tenantId": tenant_id,
        "device": dev_snap.to_dict() if dev_snap.exists else None,
        "sensorMap": sensor_map,
    }


# ---------- GET: config ----------
@app.get("/devices/{device_id}/config")
def get_device_config(device_id: str, _: None = Depends(verify_api_key)):
    tenant_id = resolve_tenant(device_id)
    dev_ref = get_device_ref(tenant_id, device_id)
    snap = dev_ref.get()

    out_cfg = {
        "intervalSec": SAMPLE_INTERVAL_SEC_DEFAULT,
        "samplesPerBatch": SAMPLES_PER_BATCH_DEFAULT,
        "schedule": []
    }

    if snap.exists:
        d = snap.to_dict() or {}
        tc = (d.get("telemetryConfig") or {})
        if isinstance(tc, dict):
            out_cfg["intervalSec"] = int(tc.get("intervalSec", out_cfg["intervalSec"]))
            out_cfg["samplesPerBatch"] = int(tc.get("samplesPerBatch", out_cfg["samplesPerBatch"]))
            sch = tc.get("schedule", [])
            if isinstance(sch, list):
                norm = []
                for x in sch[:MAX_SCHEDULE]:
                    if not isinstance(x, dict):
                        continue
                    sid = int(x.get("sensorId", 0) or 0)
                    rail = int(x.get("rail", 0) or 0)
                    warm = int(x.get("warmupMs", 0) or 0)
                    if sid <= 0:
                        continue
                    if rail < -1 or rail > 2:
                        rail = 0
                    if warm < 0:
                        warm = 0
                    if warm > 60000:
                        warm = 60000
                    norm.append({"sensorId": sid, "rail": rail, "warmupMs": warm})
                out_cfg["schedule"] = norm

    return {
        "deviceId": device_id,
        "tenantId": tenant_id,
        "telemetryConfig": out_cfg
    }

# ---------- GET: readings / dailyAgg ----------
@app.get("/tenants/{tenant_id}/sensors/{sensor_id}/readings")
def get_sensor_readings(
    tenant_id: str,
    sensor_id: str,
    range: str = Query("1d"),
    limit_n: int = Query(500, ge=1, le=5000),
    _: None = Depends(verify_api_key),
):
    start, end = get_time_window(range)
    col = db.collection(f"tenants/{tenant_id}/sensors/{sensor_id}/readings")
    q = (col.where("ts", ">=", start)
           .where("ts", "<=", end)
           .order_by("ts", direction=firestore.Query.DESCENDING)
           .limit(limit_n))
    rows = []
    for s in q.stream():
        d = s.to_dict() or {}
        rows.append({"id": s.id, **d})
    return {"tenantId": tenant_id, "sensorId": sensor_id, "range": range, "items": rows}

@app.get("/tenants/{tenant_id}/sensors/{sensor_id}/dailyAgg")
def get_sensor_daily_agg(
    tenant_id: str,
    sensor_id: str,
    days: int = Query(365, ge=1, le=3660),
    _: None = Depends(verify_api_key),
):
    end = datetime.now(timezone.utc)
    start = day_start_utc(end) - timedelta(days=days - 1)
    col = db.collection(f"tenants/{tenant_id}/sensors/{sensor_id}/dailyAgg")
    q = (col.where("day", ">=", start)
           .order_by("day", direction=firestore.Query.ASCENDING)
           .limit(days + 10))
    rows = []
    for s in q.stream():
        d = s.to_dict() or {}
        rows.append({"id": s.id, **d})
    return {"tenantId": tenant_id, "sensorId": sensor_id, "days": days, "items": rows}

# ---------- MAINTENANCE ----------
@app.post("/maintenance/purge-readings")
def purge_readings(
    older_than_days: int = Query(30, ge=1, le=3650),
    batch_size: int = Query(500, ge=1, le=500),
    dry_run: bool = Query(False),
    _: None = Depends(verify_api_key),
):
    cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)

    q = (
        db.collection_group("readings")
          .where("ts", "<", cutoff)
          .order_by("ts", direction=firestore.Query.ASCENDING)
          .limit(batch_size)
    )

    snaps = list(q.stream())
    if not snaps:
        return {"status": "ok", "cutoff": cutoff.isoformat(), "deleted": 0}

    if dry_run:
        return {
            "status": "dry_run",
            "cutoff": cutoff.isoformat(),
            "wouldDelete": len(snaps),
            "first": snaps[0].reference.path,
            "last": snaps[-1].reference.path,
        }

    b = db.batch()
    for s in snaps:
        b.delete(s.reference)
    b.commit()

    return {
        "status": "ok",
        "cutoff": cutoff.isoformat(),
        "deleted": len(snaps),
        "first": snaps[0].reference.path,
        "last": snaps[-1].reference.path,
    }
