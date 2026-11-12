#!/usr/bin/env python3
"""Demostración no destructiva: simula una alteración sobre expediente y auditoría de entrega/ y prueba que se detecta."""
import copy
import json
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

from escudo import reglas  # noqa: E402
from escudo.ledger import verificar_cadena  # noqa: E402


def main() -> None:
    carpeta = Path(sys.argv[1] if len(sys.argv) > 1 else RAIZ / "entrega")
    exps = json.loads((carpeta / "expedientes.json").read_text(encoding="utf-8"))
    entradas = [json.loads(l) for l in (carpeta / "auditoria.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]

    print("1) Estado original:")
    exp = copy.deepcopy(exps[0])
    print(f"   Expediente {exp['id']}:", reglas.verificar_expediente(exp))
    ok, seq, msg = verificar_cadena(entradas)
    print(f"   Auditoría: integra={ok} ({msg})")

    print("\n2) Simulando alteración de IP en el expediente (203.0.113.10 -> 198.51.100.99):")
    exp["evidencia"]["ip_servidor"] = "198.51.100.99"
    print(f"   Expediente {exp['id']}:", reglas.verificar_expediente(exp))

    print("\n3) Simulando alteración de una entrada intermedia en auditoria.jsonl:")
    entradas_tampered = copy.deepcopy(entradas)
    mid = min(2, len(entradas_tampered) - 1)
    entradas_tampered[mid]["datos"]["tampered"] = True
    ok2, seq2, msg2 = verificar_cadena(entradas_tampered)
    print(f"   Auditoría: integra={ok2} | seq={seq2} | detalle={msg2}")


if __name__ == "__main__":
    main()
