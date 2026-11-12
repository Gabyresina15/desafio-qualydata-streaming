#!/usr/bin/env python3
"""Verificador independiente de la entrega: cadena de auditoría y sellos de los expedientes (solo stdlib)."""
import hashlib
import json
import sys
from pathlib import Path

GENESIS = "0" * 64


def canon(o):
    return json.dumps(o, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def h(o):
    return hashlib.sha256(canon(o)).hexdigest()


def main():
    d = Path(sys.argv[1] if len(sys.argv) > 1 else "entrega")
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
    exps = json.loads((d / "expedientes.json").read_text(encoding="utf-8"))
    sellados = {e["datos"]["expediente"]: e["datos"] for e in entradas if e["tipo"] == "EXPEDIENTE_CREADO"}
    for x in exps:
        ev_ok = h(x["evidencia"]) == x["evidence_hash"]
        sello = h({"id": x["id"], "evidence_hash": x["evidence_hash"], "block_type": x["block_type"],
                   "vence_min": x["vence_min"], "parent_id": x.get("parent_id"), "tipo": x["tipo"]})
        ok = ev_ok and sello == x["sello_expediente"] and sellados.get(x["id"], {}).get("evidence_hash") == x["evidence_hash"]
        print(f"EXPEDIENTE {x['id']} [{x['block_type']}] {'OK' if ok else 'ALTERADO'}")
        mal += 0 if ok else 1
    sys.exit(1 if mal else 0)


if __name__ == "__main__":
    main()
