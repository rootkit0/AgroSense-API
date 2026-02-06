from typing import Optional, Dict
from fastapi import HTTPException, Header
import firebase_admin
from firebase_admin import auth

_initialized = False

def init_firebase():
    global _initialized
    if _initialized:
        return
    # Uses Application Default Credentials (ADC) or GOOGLE_APPLICATION_CREDENTIALS.
    firebase_admin.initialize_app()
    _initialized = True

def verify_bearer(authorization: Optional[str] = Header(default=None)) -> Dict:
    init_firebase()
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    try:
        decoded = auth.verify_id_token(token)
        return decoded
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Invalid token: {e}")
