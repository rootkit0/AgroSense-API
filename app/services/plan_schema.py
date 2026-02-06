from __future__ import annotations
from pydantic import BaseModel, Field, ConfigDict, field_validator
from typing import List, Literal, Optional

# Matches your firmware constraints
CHANNEL_COUNT = 3
MAX_FIELDS = 24
MAX_STEPS = 16
MAX_DECODE = 10
MAX_REGS_PER_STEP = 32

DecodeType = Literal["u16","s16","u32be","s32be","f32be"]

class Channel(BaseModel):
    model_config = ConfigDict(extra="ignore")
    gpio: int
    active_high: bool = True
    warmup_ms: int = 800

class Decode(BaseModel):
    model_config = ConfigDict(extra="ignore")
    idx: int
    type: DecodeType
    reg_ofs: int
    scale: float = 1.0
    offset: float = 0.0

class Modbus(BaseModel):
    model_config = ConfigDict(extra="ignore")
    addr: int
    reg: int
    count: int
    timeout_ms: int = 200

class Step(BaseModel):
    model_config = ConfigDict(extra="ignore")
    ch: int
    modbus: Modbus
    decode: List[Decode]

class Plan(BaseModel):
    model_config = ConfigDict(extra="ignore")
    ver: int = 0
    channels: List[Channel]
    fields: List[str]
    steps: List[Step]

    @field_validator("channels")
    @classmethod
    def val_channels(cls, v):
        if len(v) != CHANNEL_COUNT:
            raise ValueError("channels must have length 3")
        return v

    @field_validator("fields")
    @classmethod
    def val_fields(cls, v):
        if len(v) == 0:
            raise ValueError("fields empty")
        if len(v) > MAX_FIELDS:
            raise ValueError("too many fields")
        for name in v:
            if not isinstance(name,str) or len(name)==0 or len(name)>32:
                raise ValueError("invalid field name")
        return v

    @field_validator("steps")
    @classmethod
    def val_steps(cls, v):
        if len(v) == 0:
            raise ValueError("steps empty")
        if len(v) > MAX_STEPS:
            raise ValueError("too many steps")
        return v

def validate_plan(obj: dict) -> Plan:
    p = Plan.model_validate(obj)

    field_count = len(p.fields)
    for st in p.steps:
        if st.ch < 0 or st.ch >= CHANNEL_COUNT:
            raise ValueError("step.ch out of range")
        mb = st.modbus
        if mb.count <= 0 or mb.count > MAX_REGS_PER_STEP:
            raise ValueError("modbus.count out of range")
        if len(st.decode) == 0 or len(st.decode) > MAX_DECODE:
            raise ValueError("decode list size out of range")
        for d in st.decode:
            if d.idx < 0 or d.idx >= field_count:
                raise ValueError("decode.idx out of range")
            need = 2 if d.type in ("u32be","s32be","f32be") else 1
            if d.reg_ofs < 0 or (d.reg_ofs + need) > mb.count:
                raise ValueError("decode.reg_ofs out of range")
    return p
