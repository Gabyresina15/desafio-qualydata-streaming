"""Registro de auditoría append-only encadenado por hash (prevHash / currentHash, SHA-256)."""
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

from .util import hash_canonico

GENESIS = "0" * 64


def _calcular(entrada: dict) -> str:
    sin_hash = {k: v for k, v in entrada.items() if k != "currentHash"}
    return hash_canonico(sin_hash)


def verificar_cadena(entradas: List[dict]) -> Tuple[bool, Optional[int], str]:
    """Recorre la cadena. Devuelve (ok, seq_corrupto, mensaje)."""
    previo = GENESIS
    for i, e in enumerate(entradas):
        if e.get("seq") != i:
            return False, e.get("seq"), f"secuencia rota en la posición {i}"
        if e.get("prevHash") != previo:
            return False, e["seq"], f"prevHash no coincide en seq {e['seq']}"
        if _calcular(e) != e.get("currentHash"):
            return False, e["seq"], f"contenido alterado en seq {e['seq']}"
        previo = e["currentHash"]
    return True, None, f"cadena íntegra ({len(entradas)} entradas)"


class Ledger:
    def __init__(self, ruta: Path):
        self.lock = threading.RLock()
        self.entradas: List[dict] = []
        self.ruta = Path(ruta)
        self.ruta.parent.mkdir(parents=True, exist_ok=True)
        self.archivo = open(self.ruta, "w", encoding="utf-8")

    def agregar(self, tipo: str, datos: dict, minuto: Optional[float] = None) -> dict:
        with self.lock:
            previo = self.entradas[-1]["currentHash"] if self.entradas else GENESIS
            entrada = {
                "seq": len(self.entradas),
                "ts_utc": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                "min": None if minuto is None else round(minuto, 3),
                "tipo": tipo,
                "datos": datos,
                "prevHash": previo,
            }
            entrada["currentHash"] = _calcular(entrada)
            self.entradas.append(entrada)
            self.archivo.write(json.dumps(entrada, ensure_ascii=False, sort_keys=True) + "\n")
            self.archivo.flush()
            return entrada

    def verificar(self) -> Tuple[bool, Optional[int], str]:
        with self.lock:
            return verificar_cadena(list(self.entradas))

    def copia(self, desde_seq: int = 0) -> List[dict]:
        with self.lock:
            return list(self.entradas[desde_seq:])

    def cabeza(self) -> str:
        with self.lock:
            return self.entradas[-1]["currentHash"] if self.entradas else GENESIS
