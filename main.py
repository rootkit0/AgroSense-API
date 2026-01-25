from fastapi import FastAPI, HTTPException, Depends, Query, Header
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from datetime import datetime, timezone, timedelta
import os

import firebase_admin
from firebase_admin import credentials, firestore

# ---------- CONFIG ----------
API_KEY = os.getenv("AGROMIND_API_KEY", "iC6919i3f88i342Q")

SAMPLE_INTERVAL_SEC_DEFAULT = 900
RAW_RETENTION_DAYS = int(os.getenv("RAW_RETENTION_DAYS", "60"))

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

def get_time_window(range_key: str):
    if range_key not in RANGE_MAP:
        raise HTTPException(status_code=400, detail="Rango de tiempo no válido")
    now = datetime.now(timezone.utc)
    start = now - RANGE_MAP[range_key]
    return start, now

# ---------- MODELOS ----------
class BaseTelemetry(BaseModel):
    id: str = Field(..., description="deviceId")
    la: Optional[float] = None
    lo: Optional[float] = None
    b: Optional[float] = None
    s: Optional[float] = None
    intervalSec: Optional[int] = None

class NPKSample(BaseModel):
    n: float
    p: float
    k: float

class SoilMoistSample(BaseModel):
    v: float

class FertSample(BaseModel):
    ec: float
    st: Optional[float] = None

class HygroSample(BaseModel):
    at: float
    rh: float

class LeafSample(BaseModel):
    w: bool
    wd: Optional[float] = None

class RainSample(BaseModel):
    r: float
    ri: Optional[float] = None

class ThermalSample(BaseModel):
    tt: float

class NPKTelemetry(BaseTelemetry):
    samples: List[NPKSample]

class SoilMoistureTelemetry(BaseTelemetry):
    samples: List[SoilMoistSample]

class FertirrigationTelemetry(BaseTelemetry):
    samples: List[FertSample]

class HygrometerTelemetry(BaseTelemetry):
    samples: List[HygroSample]

class LeafWetnessTelemetry(BaseTelemetry):
    samples: List[LeafSample]

class RainGaugeTelemetry(BaseTelemetry):
    samples: List[RainSample]

class ThermalStressTelemetry(BaseTelemetry):
    samples: List[ThermalSample]

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
def day_id(dt: datetime) -> str:
    return dt.strftime("%Y%m%d")

def day_start_utc(dt: datetime) -> datetime:
    return datetime(dt.year, dt.month, dt.day, tzinfo=timezone.utc)

def values_from_sample(measurement_type: str, sample: Any) -> Dict[str, float]:
    if measurement_type == "npk":
        return {
            "nitrogen_mgkg": float(sample.n),
            "phosphorus_mgkg": float(sample.p),
            "potassium_mgkg": float(sample.k),
        }
    if measurement_type == "soil_moisture":
        return {"vwc_percent": float(sample.v)}
    if measurement_type == "fertirrigation":
        vals = {"ec_mscm": float(sample.ec)}
        if sample.st is not None:
            vals["solution_temp_c"] = float(sample.st)
        return vals
    if measurement_type == "hygrometer":
        return {"air_temp_c": float(sample.at), "rh_percent": float(sample.rh)}
    if measurement_type == "leaf_wetness":
        vals = {"wet": 1.0 if bool(sample.w) else 0.0}
        if sample.wd is not None:
            vals["wet_duration_s"] = float(sample.wd)
        return vals
    if measurement_type == "rain_gauge":
        vals = {"rainfall_mm": float(sample.r)}
        if sample.ri is not None:
            vals["intensity_mm_h"] = float(sample.ri)
        return vals
    if measurement_type == "thermal_stress":
        return {"temperature_c": float(sample.tt)}
    raise HTTPException(status_code=400, detail=f"type no soportado: {measurement_type}")

def resolve_sensor_by_index(device_id: str):
    idx_ref = db.document(f"deviceIndex/{device_id}")
    snap = idx_ref.get()
    if snap.exists:
        d = snap.to_dict() or {}
        if d.get("tenantId") and d.get("sensorId"):
            return d["tenantId"], d["sensorId"]
    return None

def write_index(device_id: str, tenant_id: str, sensor_id: str):
    db.document(f"deviceIndex/{device_id}").set(
        {"tenantId": tenant_id, "sensorId": sensor_id, "updatedAt": firestore.SERVER_TIMESTAMP},
        merge=True
    )

def resolve_sensor(device_id: str):
    idx = resolve_sensor_by_index(device_id)
    if idx:
        return idx[0], idx[1]

    docs = list(
        db.collection_group("sensors")
          .where("deviceId", "==", device_id)
          .limit(2)
          .stream()
    )
    if not docs:
        raise HTTPException(status_code=404, detail=f"Sensor no registrado para deviceId={device_id}")
    if len(docs) > 1:
        raise HTTPException(status_code=409, detail=f"deviceId duplicado en múltiples sensores: {device_id}")

    snap = docs[0]
    parts = snap.reference.path.split("/")
    tenant_id = parts[1]
    sensor_id = parts[3]

    write_index(device_id, tenant_id, sensor_id)
    return tenant_id, sensor_id

# ---------- DAILY AGG ----------
@firestore.transactional
def tx_apply_daily_agg(transaction: firestore.Transaction, agg_ref, day_ts: datetime, updates: Dict[str, Any]):
    """
    updates:
      {
        "seenIds": { "YYYYMMDDHHMM": True, ... },
        "metrics": { "temp": {"min":..,"max":..,"sum":..,"count":..}, ... }  (incremental)
      }
    Evita doble conteo si el device reintenta el mismo lote.
    """
    snap = transaction.get(agg_ref)
    doc = snap.to_dict() if snap.exists else {"day": day_ts, "metrics": {}, "seen": {}}
    doc.setdefault("day", day_ts)
    doc.setdefault("metrics", {})
    doc.setdefault("seen", {})

    seen: Dict[str, bool] = doc["seen"]
    metrics: Dict[str, Dict[str, Any]] = doc["metrics"]

    for rid, payload in updates.items():
        if rid == "_metricsByReading":
            continue

    metrics_by_reading: Dict[str, Dict[str, float]] = updates["_metricsByReading"]

    for reading_id, values in metrics_by_reading.items():
        if seen.get(reading_id):
            continue

        seen[reading_id] = True

        for k, v in values.items():
            cur = metrics.get(k) or {"min": v, "max": v, "sum": 0.0, "count": 0}
            cur["min"] = min(float(cur.get("min", v)), v)
            cur["max"] = max(float(cur.get("max", v)), v)
            cur["sum"] = float(cur.get("sum", 0.0)) + v
            cur["count"] = int(cur.get("count", 0)) + 1
            metrics[k] = cur

    doc["metrics"] = metrics
    doc["seen"] = seen
    doc["updatedAt"] = firestore.SERVER_TIMESTAMP
    transaction.set(agg_ref, doc, merge=True)

# ---------- INGEST BATCH ----------
def ingest_batch(measurement_type: str, payload: BaseTelemetry, samples: list):
    if not samples:
        raise HTTPException(status_code=400, detail="samples vacío")

    tenant_id, sensor_id = resolve_sensor(payload.id)

    now = datetime.now(timezone.utc)
    interval = int(payload.intervalSec or SAMPLE_INTERVAL_SEC_DEFAULT)
    n = len(samples)

    sensor_ref = db.document(f"tenants/{tenant_id}/sensors/{sensor_id}")

    meta = {
        "deviceId": payload.id,
        "lat": payload.la,
        "lon": payload.lo,
        "batteryPct": payload.b,
        "rssi": payload.s,
        "intervalSec": interval,
    }

    batch = db.batch()

    daily_payloads: Dict[str, Dict[str, Dict[str, float]]] = {}

    last_ts = None
    last_values = None

    for i, smp in enumerate(samples):
        ts = now - timedelta(seconds=(n - 1 - i) * interval)
        values = values_from_sample(measurement_type, smp)

        last_ts = ts
        last_values = values

        reading_id = ts.strftime("%Y%m%d%H%M")
        reading_ref = db.document(f"tenants/{tenant_id}/sensors/{sensor_id}/readings/{reading_id}")

        expires_at = ts + timedelta(days=RAW_RETENTION_DAYS)

        data = {
        "ts": ts,
        "updatedAt": firestore.SERVER_TIMESTAMP,
        "meta": meta,
        "meta.lastType": measurement_type,
        "expiresAt": expires_at,
        }
        for k, v in values.items():
            data[f"values.{k}"] = v

        batch.set(reading_ref, data, merge=True)

        did = day_id(ts)
        daily_payloads.setdefault(did, {})

        merged = daily_payloads[did].get(reading_id, {})
        merged.update(values)
        daily_payloads[did][reading_id] = merged

    batch.set(sensor_ref, {
        "status": {
            "batteryPct": payload.b,
            "rssi": payload.s,
            "lastSeenAt": now,
            "lastLat": payload.la,
            "lastLon": payload.lo,
        },
        "lastReading": {
            "ts": last_ts,
            "values": last_values,
            "type": measurement_type,
        }
    }, merge=True)

    batch.commit()

    updated_days = []
    for did, readings_map in daily_payloads.items():
        day_ts = day_start_utc(datetime.strptime(did, "%Y%m%d").replace(tzinfo=timezone.utc))
        agg_ref = db.document(f"tenants/{tenant_id}/sensors/{sensor_id}/dailyAgg/{did}")
        tx = db.transaction()
        tx_apply_daily_agg(tx, agg_ref, day_ts, {"_metricsByReading": readings_map})
        updated_days.append(did)

    return {
        "status": "success",
        "tenantId": tenant_id,
        "sensorId": sensor_id,
        "type": measurement_type,
        "ingested": n,
        "updatedDailyAggDays": updated_days,
    }

# ---------- POST endpoints ----------
@app.post("/sensors/npk")
def post_npk(data: NPKTelemetry, _: None = Depends(verify_api_key)):
    return ingest_batch("npk", data, data.samples)

@app.post("/sensors/soil-moisture")
def post_soil_moisture(data: SoilMoistureTelemetry, _: None = Depends(verify_api_key)):
    return ingest_batch("soil_moisture", data, data.samples)

@app.post("/sensors/fertirrigation")
def post_fertirrigation(data: FertirrigationTelemetry, _: None = Depends(verify_api_key)):
    return ingest_batch("fertirrigation", data, data.samples)

@app.post("/sensors/hygrometer")
def post_hygrometer(data: HygrometerTelemetry, _: None = Depends(verify_api_key)):
    return ingest_batch("hygrometer", data, data.samples)

@app.post("/sensors/leaf-wetness")
def post_leaf_wetness(data: LeafWetnessTelemetry, _: None = Depends(verify_api_key)):
    return ingest_batch("leaf_wetness", data, data.samples)

@app.post("/sensors/rain-gauge")
def post_rain_gauge(data: RainGaugeTelemetry, _: None = Depends(verify_api_key)):
    return ingest_batch("rain_gauge", data, data.samples)

@app.post("/sensors/thermal-stress")
def post_thermal_stress(data: ThermalStressTelemetry, _: None = Depends(verify_api_key)):
    return ingest_batch("thermal_stress", data, data.samples)

# ---------- GET endpoints ----------
@app.get("/devices/{device_id}/resolve")
def get_device_resolve(device_id: str, _: None = Depends(verify_api_key)):
    tenant_id, sensor_id = resolve_sensor(device_id)
    sensor_ref = db.document(f"tenants/{tenant_id}/sensors/{sensor_id}")
    snap = sensor_ref.get()
    return {
        "deviceId": device_id,
        "tenantId": tenant_id,
        "sensorId": sensor_id,
        "sensor": snap.to_dict() if snap.exists else None
    }

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

# ---------- MAINTENANCE endpoints ----------
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

@app.post("/maintenance/recompute-tenant-stats")
def recompute_tenant_stats(
    tenant_id: str = Query(...),
    stale_hours: int = Query(2, ge=1, le=168),
    low_batt: int = Query(20, ge=1, le=100),
    _: None = Depends(verify_api_key),
):
    now = datetime.now(timezone.utc)
    stale_cutoff = now - timedelta(hours=stale_hours)

    sensors_ref = db.collection(f"tenants/{tenant_id}/sensors")
    sensors_total = 0
    sensors_active = 0
    battery_low = 0

    for s in sensors_ref.stream():
      sensors_total += 1
      d = s.to_dict() or {}
      st = (d.get("status") or {})
      last_seen = st.get("lastSeenAt")
      batt = st.get("batteryPct")

      if isinstance(last_seen, datetime) and last_seen >= stale_cutoff:
        sensors_active += 1

      if isinstance(batt, (int, float)) and batt < low_batt:
        battery_low += 1

    sensors_stale = max(0, sensors_total - sensors_active)

    alerts_open = 0
    alerts_critical_open = 0
    alerts_ref = db.collection(f"tenants/{tenant_id}/alerts")
    for a in alerts_ref.where("status", "==", "open").stream():
      alerts_open += 1
      d = a.to_dict() or {}
      if d.get("severity") == "critical":
        alerts_critical_open += 1

    recs_open = 0
    recs_last24h = 0
    recs_ref = db.collection(f"tenants/{tenant_id}/aiRecommendations")

    last24h_cutoff = now - timedelta(hours=24)
    for r in recs_ref.stream():
      d = r.to_dict() or {}
      status = d.get("status", "open")
      if status != "done":
        recs_open += 1

      created = d.get("createdAt")
      if isinstance(created, datetime) and created >= last24h_cutoff:
        recs_last24h += 1

    stats_ref = db.document(f"tenants/{tenant_id}/stats/current")
    stats_ref.set({
      "updatedAt": firestore.SERVER_TIMESTAMP,
      "staleMs": stale_hours * 60 * 60 * 1000,
      "sensors": {
        "total": sensors_total,
        "active": sensors_active,
        "stale": sensors_stale,
        "batteryLow": battery_low,
      },
      "alerts": {
        "open": alerts_open,
        "criticalOpen": alerts_critical_open,
      },
      "recs": {
        "open": recs_open,
        "last24h": recs_last24h,
      }
    }, merge=True)

    return {"status": "ok", "tenantId": tenant_id}

@app.post("/maintenance/recompute-all-tenant-stats")
def recompute_all_tenant_stats(
    stale_hours: int = Query(2, ge=1, le=168),
    low_batt: int = Query(20, ge=1, le=100),
    _: None = Depends(verify_api_key),
):
    tenants = [t.id for t in db.collection("tenants").stream()]
    done = []
    for tid in tenants:
      now = datetime.now(timezone.utc)
      stale_cutoff = now - timedelta(hours=stale_hours)

      sensors_total = 0
      sensors_active = 0
      battery_low = 0
      for s in db.collection(f"tenants/{tid}/sensors").stream():
        sensors_total += 1
        d = s.to_dict() or {}
        st = (d.get("status") or {})
        last_seen = st.get("lastSeenAt")
        batt = st.get("batteryPct")
        if isinstance(last_seen, datetime) and last_seen >= stale_cutoff:
          sensors_active += 1
        if isinstance(batt, (int, float)) and batt < low_batt:
          battery_low += 1

      alerts_open = 0
      alerts_critical_open = 0
      for a in db.collection(f"tenants/{tid}/alerts").where("status", "==", "open").stream():
        alerts_open += 1
        if (a.to_dict() or {}).get("severity") == "critical":
          alerts_critical_open += 1

      recs_open = 0
      recs_last24h = 0
      last24h_cutoff = now - timedelta(hours=24)
      for r in db.collection(f"tenants/{tid}/aiRecommendations").stream():
        d = r.to_dict() or {}
        if d.get("status", "open") != "done":
          recs_open += 1
        created = d.get("createdAt")
        if isinstance(created, datetime) and created >= last24h_cutoff:
          recs_last24h += 1

      db.document(f"tenants/{tid}/stats/current").set({
        "updatedAt": firestore.SERVER_TIMESTAMP,
        "staleMs": stale_hours * 60 * 60 * 1000,
        "sensors": {
          "total": sensors_total,
          "active": sensors_active,
          "stale": max(0, sensors_total - sensors_active),
          "batteryLow": battery_low,
        },
        "alerts": {"open": alerts_open, "criticalOpen": alerts_critical_open},
        "recs": {"open": recs_open, "last24h": recs_last24h},
      }, merge=True)

      done.append(tid)

    return {"status": "ok", "tenants": done}
