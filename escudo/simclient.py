"""Cliente HTTP del simulador y reloj de partido interpolado."""
import json
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Optional, Tuple


class SimClient:
    def __init__(self, base: str, timeout: float = 5.0):
        self.base = base.rstrip("/")
        self.timeout = timeout

    def pedir(self, ruta: str, cuerpo: Optional[dict] = None) -> Tuple[int, Any]:
        datos = json.dumps(cuerpo).encode("utf-8") if cuerpo is not None else None
        req = urllib.request.Request(self.base + ruta, data=datos, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read())
            except Exception:
                return e.code, {"error": str(e)}

    def get(self, ruta: str) -> Tuple[int, Any]:
        return self.pedir(ruta)

    def post(self, ruta: str, cuerpo: dict) -> Tuple[int, Any]:
        return self.pedir(ruta, cuerpo)


class RelojPartido:
    """Minuto de partido: se sincroniza con /estado y se interpola entre consultas."""

    def __init__(self):
        self.lock = threading.Lock()
        self.minuto = 0.0
        self.mono = time.monotonic()
        self.acelerar = 1.0
        self.fin_min = 105.0
        self.finalizado = False
        self.sincronizado = False

    def sincronizar(self, estado: dict) -> None:
        with self.lock:
            self.minuto = float(estado["minuto_actual"])
            self.mono = time.monotonic()
            self.acelerar = float(estado.get("acelerar", 1.0))
            self.fin_min = float(estado.get("fin_min", 105.0))
            self.finalizado = bool(estado.get("finalizado", False))
            self.sincronizado = True

    def ahora(self) -> float:
        with self.lock:
            return self.minuto + (time.monotonic() - self.mono) * self.acelerar / 60.0

    def terminado(self) -> bool:
        with self.lock:
            return self.finalizado or (self.sincronizado and self.ahora_sin_lock() >= self.fin_min)

    def ahora_sin_lock(self) -> float:
        return self.minuto + (time.monotonic() - self.mono) * self.acelerar / 60.0
