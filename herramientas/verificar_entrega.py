#!/usr/bin/env python3
"""Verificador independiente de la entrega (stdlib):
- Cadena hash SHA-256 de auditoria.jsonl
- Sello HMAC de cabeza del ledger (LEDGER_SELLADO) y su publicación en transparencia.json
- Integridad de captura (SHA-256 de contenido_b64), evidence_hash, sello_expediente y firma_decision HMAC
"""
import argparse
import base64
import hashlib
import hmac
import json
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
GENESIS = "0" * 64


def canon(o):
    return json.dumps(o, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def h(o):
    return hashlib.sha256(canon(o)).hexdigest()


def hmac_hex(clave: str, msg: str) -> str:
    return hmac.new(clave.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256).hexdigest()


def cargar_claves(ruta_juzgados: Path) -> dict:
    datos = json.loads(ruta_juzgados.read_text(encoding="utf-8"))
    return {j["id"]: j["clave_firma_demo"] for jz in datos["juzgados"] for j in jz["jueces"]}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("carpeta", nargs="?", default=str(RAIZ / "entrega"))
    ap.add_argument("--juzgados", type=Path, default=RAIZ / "config" / "juzgados.json")
    args = ap.parse_args()

    d = Path(args.carpeta)
    claves = cargar_claves(args.juzgados)
    entradas = [json.loads(l) for l in (d / "auditoria.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]

    previo, mal = GENESIS, 0
    for i, e in enumerate(entradas):
        sin = {k: v for k, v in e.items() if k != "currentHash"}
        if e["seq"] != i or e["prevHash"] != previo or h(sin) != e["currentHash"]:
            print(f"AUDITORÍA: entrada {i} corrupta")
            mal += 1
            break
        previo = e["currentHash"]
    else:
        print(f"AUDITORÍA: cadena íntegra, {len(entradas)} entradas, cabeza {previo[:16]}…")

    sellos = [e for e in entradas if e["tipo"] == "LEDGER_SELLADO"]
    if not sellos:
        print("SELLO LEDGER: falta evento LEDGER_SELLADO")
        mal += 1
    else:
        ult = sellos[-1]
        dat = ult["datos"]
        clave = claves.get(dat.get("juez_id"), "")
        esp = hmac_hex(clave, f"LEDGER|{dat.get('cabeza_sellada')}|{dat.get('juez_id')}|{dat.get('motivo')}")
        sello_ok = bool(clave) and hmac.compare_digest(esp, dat.get("firma_ledger", "")) and dat.get("cabeza_sellada") == ult.get("prevHash")
        print(f"SELLO LEDGER: {'OK' if sello_ok else 'INVÁLIDO'} (juez {dat.get('juez_id')})")
        mal += 0 if sello_ok else 1

    tr_path = d / "transparencia.json"
    if not tr_path.exists():
        print("TRANSPARENCIA: falta transparencia.json")
        mal += 1
    else:
        tr = json.loads(tr_path.read_text(encoding="utf-8"))
        tr_ok = tr.get("hash_cabeza_ledger") == previo
        print(f"TRANSPARENCIA: {'OK' if tr_ok else 'DESINCRONIZADA'} ({len(tr.get('ordenes', []))} órdenes, hash {str(tr.get('hash_cabeza_ledger'))[:16]}…)")
        mal += 0 if tr_ok else 1

    exps = json.loads((d / "expedientes.json").read_text(encoding="utf-8"))
    sellados = {e["datos"]["expediente"]: e["datos"] for e in entradas if e["tipo"] == "EXPEDIENTE_CREADO"}
    firmas_log = {e["datos"]["expediente"]: e["datos"] for e in entradas if e["tipo"] == "FIRMA_JUDICIAL"}
    for x in exps:
        cap = x["evidencia"]["captura"]
        cap_sha_ok = hashlib.sha256(base64.b64decode(cap["contenido_b64"])).hexdigest() == cap["sha256"]
        ev_ok = h(x["evidencia"]) == x["evidence_hash"]
        sello = h({"id": x["id"], "evidence_hash": x["evidence_hash"], "block_type": x["block_type"],
                   "vence_min": x["vence_min"], "parent_id": x.get("parent_id"), "tipo": x["tipo"]})
        sello_ok = sello == x["sello_expediente"] and sellados.get(x["id"], {}).get("evidence_hash") == x["evidence_hash"]

        firma_ok = True
        if x.get("firmante"):
            dec = x.get("decision") or ("rechazar" if x["estado"] == "REJECTED" else "aprobar")
            clave = claves.get(x["firmante"], "")
            esp_f = hmac_hex(clave, f"DECISION|{x['id']}|{x['evidence_hash']}|{dec}|{x['firmante']}")
            firma_ok = bool(clave) and hmac.compare_digest(esp_f, x.get("firma_decision", "")) and firmas_log.get(x["id"], {}).get("firma_decision") == x.get("firma_decision")

        ok = cap_sha_ok and ev_ok and sello_ok and firma_ok
        print(f"EXPEDIENTE {x['id']} [{x['block_type']} | {x.get('firmado_via')}] {'OK' if ok else 'ALTERADO'} "
              f"(captura={'ok' if cap_sha_ok else 'BAD'}, firma={'ok' if firma_ok else 'BAD'})")
        mal += 0 if ok else 1
    sys.exit(1 if mal else 0)


if __name__ == "__main__":
    main()
