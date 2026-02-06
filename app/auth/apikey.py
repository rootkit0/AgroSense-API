import os
import hmac
from fastapi import Security, HTTPException
from fastapi.security.api_key import APIKeyHeader

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

def require_ingest_key(api_key: str = Security(api_key_header)) -> None:
    expected = os.getenv("INGEST_API_KEY", "")
    if not expected:
        raise RuntimeError("INGEST_API_KEY not set")
    if not api_key:
        raise HTTPException(status_code=401, detail="Missing API key")
    if not hmac.compare_digest(api_key, expected):
        raise HTTPException(status_code=403, detail="Invalid API key")
