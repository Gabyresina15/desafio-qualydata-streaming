"""Tests de integración de Plataforma (turnos, reclamos y UI) con un SimClient en memoria."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

from escudo.plataforma import Error, Plataforma  # noqa: E402
from escudo.web import crear_servidor  # noqa: E402


class FakeSim:
    def __init__(self):
        self.minuto = 45.0
        self.turno = ["jueza-laura-ficticia"]
        self.dns_ips = {
            "203.0.113.10": {"ip": "203.0.113.10", "asn": 64600, "organizacion": "Bruma Hosting Dedicado",
                             "dominios": ["s1.pelotaya.test"]},
        }
        self.levantadas = []
        self.enviadas = []
        self.ordenes_isp = {"aurora": []}

    def get(self, ruta: str):
        if ruta == "/estado":
            return 200, {"partido": "test-01", "minuto_actual": self.minuto, "fin_min": 105.0,
                         "finalizado": False, "acelerar": 1.0, "jueces_de_turno": list(self.turno)}
        if ruta == "/partido":
            return 200, {"id": "test-01", "fin_min": 105,
                         "marcas_de_agua_validas": [{"prefijo": "LP26-PRA-OFI"}],
                         "licenciatarios": []}
        if ruta == "/proveedores":
            return 200, [{"id": "aurora", "nombre": "Aurora", "capacidades": ["ip", "dominio"]}]
        if ruta == "/juzgados":
            return 200, {"turnos": [
                {"juez_id": "jueza-laura-ficticia", "desde_min": -60, "hasta_min": 50},
                {"juez_id": "juez-martin-ejemplo", "desde_min": 50, "hasta_min": 240},
            ]}
        if ruta.startswith("/dns-pasivo?ip="):
            ip = ruta.split("=", 1)[1]
            if ip in self.dns_ips:
                return 200, self.dns_ips[ip]
            return 404, {"error": "sin datos"}
        if ruta == "/proveedores/aurora/ordenes":
            return 200, list(self.ordenes_isp["aurora"])
        if ruta.startswith("/reclamos"):
            return 200, {"minuto_actual": self.minuto, "reclamos": []}
        return 404, {}

    def post(self, ruta: str, cuerpo: dict):
        if ruta == "/proveedores/aurora/ordenes":
            self.enviadas.append(cuerpo)
            self.ordenes_isp["aurora"].append({**cuerpo, "estado": "aplicada", "aplicada_min": self.minuto})
            return 202, {"orden_id": cuerpo["orden_id"], "estado": "recibida"}
        if ruta.endswith("/levantar"):
            oid = ruta.split("/")[4]
            self.levantadas.append((oid, cuerpo))
            for o in self.ordenes_isp["aurora"]:
                if o["orden_id"] == oid:
                    o["estado"] = "levantada"
            return 200, {"orden_id": oid, "estado": "levantada"}
        return 404, {}


def hacer_plat(tmp: Path) -> tuple[Plataforma, FakeSim]:
    sim = FakeSim()
    with patch("escudo.plataforma.SimClient", return_value=sim):
        plat = Plataforma("http://fake", RAIZ / "config" / "juzgados.json",
                          RAIZ / "config" / "usuarios_demo.json", tmp / "sal", tmp / "ent")
    _, plat.partido = sim.get("/partido")
    _, plat.proveedores = sim.get("/proveedores")
    plat._tick_turnos()
    return plat, sim


class IntegracionTest(unittest.TestCase):
    def test_cambio_de_turno_revoca_y_reasigna(self):
        with tempfile.TemporaryDirectory() as td:
            plat, sim = hacer_plat(Path(td))
            try:
                tk_laura = plat.login("jueza-laura-ficticia", "laura-demo-2026")["token"]
                with self.assertRaises(Error):
                    plat.login("jueza-laura-ficticia", "clave-mala")

                plat.procesar_deteccion({
                    "id": "D-1", "minuto": 45.0, "host_stream": "s1.pelotaya.test",
                    "ip_servidor": "203.0.113.10", "asn": 64600,
                    "marca_de_agua_extraida": "LP26-PRA-OFI-S1",
                    "pagina": "https://pelotaya.test/vivo",
                    "captura": {"formato": "mpegts", "duracion_seg": 6, "capturada_min": 45.0, "contenido_b64": "R78w"},
                })
                exp_id = next(iter(plat.expedientes))
                self.assertEqual(plat.expedientes[exp_id]["juez_asignado"], "jueza-laura-ficticia")

                # Cambio de turno al minuto 51
                sim.minuto = 51.0
                sim.turno = ["juez-martin-ejemplo"]
                plat._tick_turnos()

                with self.assertRaises(Error) as ctx:
                    plat.firmar_expediente(tk_laura, exp_id, "aprobar")
                self.assertEqual(ctx.exception.codigo, 401)
                self.assertEqual(plat.expedientes[exp_id]["juez_asignado"], "juez-martin-ejemplo")

                tk_martin = plat.login("juez-martin-ejemplo", "martin-demo-2026")["token"]
                res = plat.firmar_expediente(tk_martin, exp_id, "aprobar")
                self.assertEqual(res["estado"], "DISPATCHING")
            finally:
                plat.detener()
                plat.ledger.archivo.close()
                plat.pool.shutdown(wait=True)

    def test_reclamo_levanta_ip_y_reemplaza_por_dominio(self):
        with tempfile.TemporaryDirectory() as td:
            plat, sim = hacer_plat(Path(td))
            try:
                tk = plat.login("jueza-laura-ficticia", "laura-demo-2026")["token"]
                plat.procesar_deteccion({
                    "id": "D-1", "minuto": 10.0, "host_stream": "s1.pelotaya.test",
                    "ip_servidor": "203.0.113.10", "asn": 64600,
                    "marca_de_agua_extraida": "LP26-PRA-OFI-S1",
                    "pagina": "https://pelotaya.test/vivo",
                    "captura": {"formato": "mpegts", "duracion_seg": 6, "capturada_min": 10.0, "contenido_b64": "R78w"},
                })
                exp_id = next(iter(plat.expedientes))
                plat.firmar_expediente(tk, exp_id, "aprobar")
                plat.pool.shutdown(wait=True)
                from concurrent.futures import ThreadPoolExecutor
                plat.pool = ThreadPoolExecutor(max_workers=8)
                plat._tick_ordenes()

                # La IP pasa a alojar un sitio legítimo y entra un reclamo
                sim.dns_ips["203.0.113.10"]["dominios"].append("panaderia.test")
                with plat.lock:
                    plat._abrir_reclamo("R-001", "panaderia.test", "aurora", "Sitio caído", 20.0, "ISP_FEED")
                self.assertEqual(plat.reclamos["R-001"]["estado"], "ABIERTO")

                plat.resolver_reclamo(tk, "R-001", "levantar")
                plat.pool.shutdown(wait=True)

                self.assertEqual(plat.reclamos["R-001"]["estado"], "RESUELTO")
                self.assertTrue(any(oid.endswith("-i1") for oid, _ in sim.levantadas))
                reemplazos = [e for e in plat.expedientes.values() if e["tipo"] == "REEMPLAZO_RECLAMO"]
                self.assertEqual(len(reemplazos), 1)
                self.assertEqual(reemplazos[0]["block_type"], "DOMAIN_ONLY")
            finally:
                plat.detener()
                plat.ledger.archivo.close()
                plat.pool.shutdown(wait=True)

    def test_ui_y_auditoria_http(self):
        import urllib.request
        with tempfile.TemporaryDirectory() as td:
            plat, _ = hacer_plat(Path(td))
            srv = crear_servidor(plat, 0, host="127.0.0.1")
            port = srv.server_address[1]
            import threading
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/") as r:
                    self.assertEqual(r.status, 200)
                    self.assertIn(b"Escudo Live", r.read())
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/auditoria/verificar") as r:
                    data = json.loads(r.read())
                    self.assertTrue(data["integra"])
            finally:
                srv.shutdown()
                srv.server_close()
                plat.detener()
                plat.ledger.archivo.close()
                plat.pool.shutdown(wait=True)


if __name__ == "__main__":
    unittest.main()
