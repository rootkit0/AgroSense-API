# AgroMind Telemetry API (FastAPI + Firestore)

API para ingerir telemetría IoT desde nodos (ESP32 + SIM7000G) y almacenarla en Firestore con estructura multi-tenant:

- `tenants/{tenantId}/sensors/{sensorId}`
- `tenants/{tenantId}/sensors/{sensorId}/readings/{YYYYMMDDHHMM}`
- `tenants/{tenantId}/sensors/{sensorId}/dailyAgg/{YYYYMMDD}`

La app Angular consume:
- **Raw** (`readings`) para 24h / 7d
- **Agregado diario** (`dailyAgg`) para 1 año

---

## Requisitos

- Python 3.10+
- Cuenta de servicio de Firebase (Admin SDK) en `firebase-account.json`
- Proyecto Firestore habilitado

Instalar dependencias:

```bash
pip install fastapi uvicorn firebase-admin pydantic
```

## Variables de entorno

AGROMIND_API_KEY: API Key requerida para escribir/leer.

Ejemplo:

```bash
pip install fastapi uvicorn firebase-admin pydantic
```

## Ejecutar en local

```bash
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

Healthcheck:
```bash
curl http://localhost:8000/
```

## Estructura Firestore esperada

Cada dispositivo corresponde a 1 doc de sensor.

Path:
tenants/{tenantId}/sensors/{sensorId}

Campos mínimos requeridos:
```bash
{
  "deviceId": "000001",
  "name": "Nodo 000001",
  "fieldId": "field_1"
}
```

La API resuelve tenantId y sensorId buscando este doc por deviceId.

Para acelerar, la API mantiene un índice:
deviceIndex/{deviceId} -> { tenantId, sensorId }

## Autenticación

Se admite:

- Query param: ?k=<API_KEY>
- Header: X-API-Key: <API_KEY>

## Endpoints (POST) — Ingesta batch

El firmware envía un POST por tipo con un array samples[] (sin timestamps por muestra).
La API reconstruye timestamps con intervalSec (default 300s) y genera ids deterministas YYYYMMDDHHMM (idempotencia por reintento).

## 1) NPK

POST /sensors/npk?k=API_KEY

Body:
```bash
{
  "id": "000001",
  "la": 41.123456,
  "lo": 2.123456,
  "b": 78.3,
  "s": -89.0,
  "intervalSec": 300,
  "samples": [
    {"n": 10, "p": 5, "k": 20},
    {"n": 11, "p": 5, "k": 21}
  ]
}
```
## 2) Soil Moisture

POST /sensors/soil-moisture?k=API_KEY

Body:
```bash
{
  "id": "000001",
  "samples": [{"v": 20.1}, {"v": 20.3}]
}
```
## 3) Fertirrigation

POST /sensors/fertirrigation?k=API_KEY

Body:
```bash
{
  "id": "000001",
  "samples": [{"ec": 1.23, "st": 22.1}]
}
```
## 4) Hygrometer

POST /sensors/hygrometer?k=API_KEY

Body:
```bash
{
  "id": "000001",
  "samples": [{"at": 25.2, "rh": 55.1}]
}
```
## 5) Leaf Wetness

POST /sensors/leaf-wetness?k=API_KEY

Body:
```bash
{
  "id": "000001",
  "samples": [{"w": true, "wd": 120}]
}
```
## 6) Rain Gauge

POST /sensors/rain-gauge?k=API_KEY

Body:
```bash
{
  "id": "000001",
  "samples": [{"r": 1.2, "ri": 0.5}]
}
```
## 7) Thermal Stress

POST /sensors/thermal-stress?k=API_KEY

Body:
```bash
{
  "id": "000001",
  "samples": [{"tt": 29.5}]
}
```

## Qué escribe la API en Firestore

## A) Raw readings (para 24h/7d)

tenants/{t}/sensors/{s}/readings/{YYYYMMDDHHMM}

Ejemplo:
```bash
{
  "ts": "2026-01-23T12:35:00Z",
  "values": {
    "vwc_percent": 20.3,
    "air_temp_c": 25.2
  },
  "meta": {
    "deviceId": "000001",
    "batteryPct": 78.3,
    "rssi": -89.0,
    "intervalSec": 300,
    "lastType": "soil_moisture"
  },
  "updatedAt": "<serverTimestamp>"
}
```

Cada endpoint escribe values.<metricKey> con merge para no pisar métricas de otros endpoints en el mismo minuto.

## B) Sensor doc (estado + lastReading)

tenants/{t}/sensors/{s}

Actualiza:

- status.{batteryPct,rssi,lastSeenAt,lastLat,lastLon}
- lastReading.{ts,values,type}

## C) dailyAgg (para 1 año barato)

tenants/{t}/sensors/{s}/dailyAgg/{YYYYMMDD}

Ejemplo:
```bash
{
  "day": "2026-01-23T00:00:00Z",
  "metrics": {
    "vwc_percent": {"min": 19.8, "max": 21.0, "sum": 240.5, "count": 12}
  },
  "seen": { "202601231200": true, "...": true },
  "updatedAt": "<serverTimestamp>"
}
```

La clave seen evita doble conteo si el dispositivo reintenta el mismo batch.

## Endpoints (GET) — Debug / Lectura

Resolver mapping de dispositivo

GET /devices/{deviceId}/resolve?k=API_KEY

Respuesta:
```bash
{
  "deviceId": "000001",
  "tenantId": "t1",
  "sensorId": "s1",
  "sensor": { "...": "..." }
}
```

## Leer readings por rango

GET /tenants/{tenantId}/sensors/{sensorId}/readings?range=1d&limit_n=500&k=API_KEY

range: 1h | 6h | 12h | 1d | 1w | 1m | 3m | 6m | 1y

## Leer dailyAgg (días)

GET /tenants/{tenantId}/sensors/{sensorId}/dailyAgg?days=365&k=API_KEY