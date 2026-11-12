"""Utilidades criptográficas y de serialización canónica (solo stdlib)."""
import hashlib
import hmac
import json


def canonico(obj) -> bytes:
    """JSON canónico: claves ordenadas, sin espacios, UTF-8. Base de todos los hashes."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_hex(datos: bytes) -> str:
    return hashlib.sha256(datos).hexdigest()


def hash_canonico(obj) -> str:
    return sha256_hex(canonico(obj))


def hmac_hex(clave: str, mensaje: str) -> str:
    return hmac.new(clave.encode("utf-8"), mensaje.encode("utf-8"), hashlib.sha256).hexdigest()


def mensaje_orden(o: dict) -> str:
    """Texto que firma el juez para una orden (formato del simulador)."""
    return f"{o['orden_id']}|{o['tipo']}|{o['objetivo']}|{o['vence_min']}|{o['juez_id']}"


def mensaje_levantar(orden_id: str, juez_id: str) -> str:
    return f"LEVANTAR|{orden_id}|{juez_id}"
