import orjson
import zlib

def canonical_json_bytes(obj: dict) -> bytes:
    return orjson.dumps(obj, option=orjson.OPT_SORT_KEYS)

def crc32_hex(data: bytes) -> str:
    return f"{zlib.crc32(data) & 0xFFFFFFFF:08x}"
