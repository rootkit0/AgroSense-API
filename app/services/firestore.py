from google.cloud import firestore
import secrets

db = firestore.Client()

def normalize_hw(hw: str) -> str:
    return hw.strip().upper()

def generate_hw_id() -> str:
    # 24-bit random -> 6 hex (uppercase)
    return f"{secrets.randbelow(1<<24):06X}"

def hardware_index_ref(hardware_id: str):
    return db.collection("hardwareIndex").document(normalize_hw(hardware_id))

def resolve_hardware(hardware_id: str) -> tuple[str, str]:
    snap = hardware_index_ref(hardware_id).get()
    if not snap.exists:
        raise KeyError("hardwareId not found")
    d = snap.to_dict() or {}
    return d["tenantId"], d["sensorId"]

def sensor_ref(tenant_id: str, sensor_id: str):
    return db.collection("tenants").document(tenant_id).collection("sensors").document(sensor_id)

def configs_col(tenant_id: str, sensor_id: str):
    return sensor_ref(tenant_id, sensor_id).collection("configs")

def readings_col(tenant_id: str, sensor_id: str):
    return sensor_ref(tenant_id, sensor_id).collection("readings")

def acks_col(tenant_id: str, sensor_id: str):
    return sensor_ref(tenant_id, sensor_id).collection("acks")
