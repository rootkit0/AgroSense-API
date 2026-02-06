from fastapi import HTTPException
from app.services.firestore import db

ROLE_RANK = {
    "farmer": 1,
    "tech": 2,
    "admin": 3,
}

MULTI_TENANT_ROLES = {"admin", "tech"}

def authorize_tenant(tenant_id: str, decoded_token: dict, min_role: str = "farmer") -> str:
    """Authorize access to a tenant using global users/{uid} doc (as per your screenshot)."""
    uid = decoded_token.get("uid")
    if not uid:
        raise HTTPException(status_code=401, detail="Missing uid")

    user_ref = db.collection("users").document(uid)
    snap = user_ref.get()
    if not snap.exists:
        raise HTTPException(status_code=403, detail="User profile not found")

    data = snap.to_dict() or {}
    prefs = data.get("preferences") or {}
    role = (prefs.get("role") or "").strip().lower()
    if role not in ROLE_RANK:
        raise HTTPException(status_code=403, detail="Invalid role")

    needed = (min_role or "farmer").strip().lower()
    if needed not in ROLE_RANK:
        raise HTTPException(status_code=500, detail="Server role config error")

    tenant_single = data.get("tenantId")
    tenant_ids = data.get("tenantIds")

    is_member = False
    if isinstance(tenant_single, str) and tenant_single == tenant_id:
        is_member = True

    if isinstance(tenant_ids, list) and tenant_id in tenant_ids:
        if role in MULTI_TENANT_ROLES:
            is_member = True
        else:
            if len(tenant_ids) == 1:
                is_member = True
            else:
                is_member = False

    if not is_member:
        raise HTTPException(status_code=403, detail="User not allowed for this tenant")

    if ROLE_RANK[role] < ROLE_RANK[needed]:
        raise HTTPException(status_code=403, detail="Insufficient role")

    return role
