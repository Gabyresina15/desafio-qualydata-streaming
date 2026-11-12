import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from escudo import reglas  # noqa: E402
from escudo.ledger import Ledger, verificar_cadena  # noqa: E402

PARTIDO = {"id": "t", "fin_min": 105,
           "marcas_de_agua_validas": [{"prefijo": "LP26-PRA-OFI"}, {"prefijo": "LP26-PRA-LIC-NORTE"}],
           "licenciatarios": [{"nombre": "Canal Norte", "prefijo_marca": "LP26-PRA-LIC-NORTE",
                               "dominios_autorizados": ["canalnorte.test", "envivo.canalnorte.test"]}]}


def det(host="s1.pelotaya.test", ip="203.0.113.10", marca="LP26-PRA-OFI-S1"):
    return {"id": "D-1", "minuto": 1.0, "host_stream": host, "ip_servidor": ip, "asn": 64600,
            "marca_de_agua_extraida": marca, "pagina": "https://pelotaya.test/x",
            "captura": {"formato": "mpegts", "duracion_seg": 6, "capturada_min": 1.0, "contenido_b64": "R78w"}}


class Reglas(unittest.TestCase):
    def test_senal_oficial(self):
        self.assertTrue(reglas.verificar_senal(det(), PARTIDO)["ok"])

    def test_ignora_licenciatario_y_otras_marcas(self):
        self.assertEqual(reglas.verificar_senal(det(marca="LP26-PRA-LIC-NORTE"), PARTIDO)["codigo"], "MARCA_DE_LICENCIATARIO")
        self.assertEqual(reglas.verificar_senal(det(marca="LP25-F03-OFI-S9"), PARTIDO)["codigo"], "MARCA_AJENA_AL_PARTIDO")
        self.assertEqual(reglas.verificar_senal(det(marca=None), PARTIDO)["codigo"], "SIN_MARCA_DE_AGUA")
        self.assertEqual(reglas.verificar_senal(det(host="envivo.canalnorte.test"), PARTIDO)["codigo"], "DOMINIO_DE_LICENCIATARIO")

    def test_prefijo_no_es_substring(self):
        self.assertFalse(reglas.verificar_senal(det(marca="LP26-PRA-OFIX-1"), PARTIDO)["ok"])

    def test_ip_dedicada(self):
        c = reglas.clasificar_bloqueo(det(), PARTIDO, 200, {"dominios": ["s1.pelotaya.test", "s1-respaldo.pelotaya.test"],
                                                             "organizacion": "Bruma Hosting Dedicado", "asn": 64600})
        self.assertEqual(c["block_type"], reglas.IP_AND_DOMAIN)

    def test_ip_compartida_o_cdn(self):
        c = reglas.clasificar_bloqueo(det(host="live.golazo.test"), PARTIDO, 200,
                                      {"dominios": ["live.golazo.test", "panaderia.test"], "organizacion": "NubeRápida CDN", "asn": 64600})
        self.assertEqual(c["block_type"], reglas.DOMAIN_ONLY)
        self.assertEqual(reglas.clasificar_bloqueo(det(), PARTIDO, 404, {})["block_type"], reglas.DOMAIN_ONLY)
        c2 = reglas.clasificar_bloqueo(det(), PARTIDO, 200, {"dominios": ["s1.pelotaya.test"], "organizacion": "Hosting Compartido", "asn": 64600})
        self.assertEqual(c2["block_type"], reglas.DOMAIN_ONLY)

    def test_sello_detecta_alteracion(self):
        ev = reglas.armar_evidencia(det(), {"prefijo_oficial": "LP26-PRA-OFI"}, 200, {"dominios": ["s1.pelotaya.test"], "asn": 64600}, PARTIDO, 1.0)
        exp = {"id": "E1", "evidencia": ev, "evidence_hash": reglas.sello(ev), "block_type": "DOMAIN_ONLY",
               "vence_min": 105, "parent_id": None, "tipo": "ORIGINAL"}
        exp["sello_expediente"] = reglas.sello_expediente(exp)
        self.assertTrue(all(reglas.verificar_expediente(exp).values()))
        ev["ip_servidor"] = "1.2.3.4"
        self.assertFalse(reglas.verificar_expediente(exp)["evidencia_integra"])


class LedgerTest(unittest.TestCase):
    def test_cadena(self):
        with tempfile.TemporaryDirectory() as d:
            lg = Ledger(Path(d) / "a.jsonl")
            for i in range(5):
                lg.agregar("X", {"i": i}, float(i))
            self.assertTrue(lg.verificar()[0])
            copia = json.loads(json.dumps(lg.copia()))
            copia[2]["datos"]["i"] = 99
            ok, seq, _ = verificar_cadena(copia)
            self.assertFalse(ok)
            self.assertEqual(seq, 2)
            copia2 = lg.copia()
            del copia2[1]
            self.assertFalse(verificar_cadena(copia2)[0])
            lg.archivo.close()


if __name__ == "__main__":
    unittest.main()
