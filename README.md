# AgroSense API (FastAPI)

Backend intermedio entre:
- **Angular Admin** (Firebase Auth) -> API (admin endpoints) -> Firestore + MQTT retained
- **Node-RED** (API key) -> API (ingest endpoints) -> Firestore

## TL;DR (local)
1) `python -m venv .venv && source .venv/bin/activate`
2) `pip install -r requirements.txt`
3) Copia `.env.example` a `.env` y rellena `INGEST_API_KEY`
4) Asegura credenciales de Google:
   - `export GOOGLE_APPLICATION_CREDENTIALS=/path/serviceAccount.json`
5) `bash run.sh`

## Firestore model (alineado con tu estructura)
- `tenants/{tenantId}/sensors/{sensorId}` incluye `hardwareId`
- Índice global: `hardwareIndex/{hardwareId} -> {tenantId, sensorId, fieldId}`
- Configs: `tenants/{tenantId}/sensors/{sensorId}/configs/{ver}`
- Readings: `tenants/{tenantId}/sensors/{sensorId}/readings/{readingId}`

## Auth / Roles (según tu screenshot)
Este API asume un doc global:
- `users/{uid}` contiene:
  - `preferences.role` en `admin|tech|farmer`
  - `tenantId` (string o null)
  - `tenantIds` (array<string> o ausente)

Regla:
- miembro si `tenantId == tenantId_param` **o** `tenantId_param in tenantIds`
- `admin`/`tech` pueden multi-tenant (`tenantIds`)
- `farmer` se limita a un tenant (si trae varios, se rechaza)

## Endpoints
### Admin (Firebase ID token)
- `POST /tenants/{tenantId}/sensors`  (min_role=tech)
- `POST /tenants/{tenantId}/sensors/{sensorId}/configs:publish` (min_role=tech)
- `POST /tenants/{tenantId}/sensors/{sensorId}/configs/{ver}:republish` (min_role=tech)

### Ingest (Node-RED API key)
- `POST /sensors/telemetry/{hardwareId}`  header `X-API-Key`
- `POST /sensors/ack/{hardwareId}` header `X-API-Key`

## MQTT publish (config)
Publica retained, en este orden:
1) `/sensors/config/<hardwareId>` (payload: plan json canonical/minificado)
2) `/sensors/config-meta/<hardwareId>` (payload: {"ver":X,"cc":"<crc32hex>"})

## Notas
- Aunque tu servicio interno sea HTTP, si está detrás de Cloudflared/Cloudflare, el cliente hablará HTTPS con el dominio.
- Este proyecto usa `orjson` para canonicalizar con `OPT_SORT_KEYS` y CRC32 sobre el string exacto publicado.
