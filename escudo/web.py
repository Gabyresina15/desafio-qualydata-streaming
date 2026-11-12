"""API HTTP de la plataforma y servidor de la UI táctica del juez."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .plataforma import Error
from .ledger import verificar_cadena

UI = Path(__file__).resolve().parent.parent / "ui" / "index.html"


def crear_servidor(plat, puerto: int, host: str = "0.0.0.0") -> ThreadingHTTPServer:
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, codigo, datos):
            cuerpo = json.dumps(datos, ensure_ascii=False).encode("utf-8")
            self.send_response(codigo)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(cuerpo)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(cuerpo)

        def _token(self):
            h = self.headers.get("Authorization", "")
            return h[7:] if h.startswith("Bearer ") else None

        def _cuerpo(self):
            n = int(self.headers.get("Content-Length", "0") or 0)
            try:
                return json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                raise Error(400, "JSON inválido")

        def _manejar(self, fn):
            try:
                self._json(200, fn())
            except Error as e:
                self._json(e.codigo, {"error": e.mensaje})
            except Exception as e:  # noqa
                self._json(500, {"error": repr(e)})

        def do_GET(self):
            url = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            p = url.path
            if p in ("/", "/index.html"):
                cuerpo = UI.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(cuerpo)))
                self.end_headers()
                self.wfile.write(cuerpo)
                return
            if p == "/api/snapshot":
                return self._manejar(lambda: plat.snapshot(self._token(), int(q.get("desde", "0"))))
            if p == "/api/transparencia":
                return self._manejar(plat.transparencia_publica)
            if p == "/api/auditoria":
                return self._manejar(lambda: plat.ledger.copia())
            if p == "/api/auditoria/verificar":
                return self._manejar(lambda: dict(zip(("integra", "seq_corrupto", "detalle"), plat.ledger.verificar()),
                                                  entradas=len(plat.ledger.entradas), hash_cabeza=plat.ledger.cabeza()))
            if p.startswith("/api/expedientes/"):
                def det():
                    plat.sesion(self._token())
                    exp = plat.expedientes.get(p.rsplit("/", 1)[1])
                    if not exp:
                        raise Error(404, "Expediente inexistente")
                    return plat.vista_exp(exp, detalle=True)
                return self._manejar(det)
            self._json(404, {"error": "ruta inexistente"})

        def do_POST(self):
            p = urlparse(self.path).path
            partes = [x for x in p.split("/") if x]
            def accion():
                b = self._cuerpo()
                if p == "/api/login":
                    return plat.login(b.get("juez_id"), b.get("password"))
                if p == "/api/logout":
                    plat.logout(self._token() or "")
                    return {"ok": True}
                if p == "/api/auto":
                    return plat.set_auto(self._token(), bool(b.get("activar")))
                if len(partes) == 4 and partes[:2] == ["api", "expedientes"] and partes[3] == "firmar":
                    return plat.firmar_expediente(self._token(), partes[2], b.get("decision", "aprobar"))
                if len(partes) == 4 and partes[:2] == ["api", "reclamos"] and partes[3] == "resolver":
                    return plat.resolver_reclamo(self._token(), partes[2], b.get("decision", "levantar"))
                if p == "/api/exportar":
                    plat._exportar("manual")
                    return {"ok": True}
                raise Error(404, "ruta inexistente")
            self._manejar(accion)

    return ThreadingHTTPServer((host, puerto), H)
