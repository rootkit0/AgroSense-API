import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

from app.routers import admin, ingest

load_dotenv()

app = FastAPI(title="AgroMind Sense API", version="0.1.0")

origins = os.getenv("CORS_ORIGINS", "*")
if origins == "*" or origins.strip() == "":
    allow_origins = ["*"]
else:
    allow_origins = [o.strip() for o in origins.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(admin.router, tags=["admin"])
app.include_router(ingest.router, tags=["ingest"])

@app.get("/health")
def health():
    return {"ok": True}
