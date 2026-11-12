#!/usr/bin/env python3
"""Operador de demostración: inicia sesión con los usuarios demo de los jueces y activa el autodespacho asistido.

Equivale a que cada juez ingrese a la UI y active el interruptor "Autodespacho asistido" durante su turno.
Se vuelve a conectar solo si la sesión se invalida; tras el fin de turno el login queda rechazado por la plataforma.
"""
import argparse
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent


def pedir(base, ruta, cuerpo=None, token=None):
    h = {"Content-Type": "application/json"}
    if token:
        h["Authorization"] = "Bearer " + token
    req = urllib.request.Request(base + ruta, data=json.dumps(cuerpo).encode() if cuerpo is not None else None, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plataforma", default="http://localhost:8090")
    ap.add_argument("--usuarios", type=Path, default=RAIZ / "config" / "usuarios_demo.json")
    ap.add_argument("--manual", action="store_true", help="solo inicia sesión; no activa el autodespacho")
    a = ap.parse_args()
    usuarios = json.loads(a.usuarios.read_text(encoding="utf-8"))["usuarios"]
    tokens = {}
    while True:
        try:
            for juez, pwd in usuarios.items():
                tk = tokens.get(juez)
                if tk:
                    st, snap = pedir(a.plataforma, "/api/snapshot", token=tk)
                    if st == 200 and snap.get("yo") and not snap["yo"]["revocada"]:
                        continue
                st, r = pedir(a.plataforma, "/api/login", {"juez_id": juez, "password": pwd})
                if st == 200:
                    tokens[juez] = r["token"]
                    if not a.manual:
                        pedir(a.plataforma, "/api/auto", {"activar": True}, r["token"])
                    print(f"[demo] sesión de {juez} {'(manual)' if a.manual else 'con autodespacho'}", flush=True)
                else:
                    tokens.pop(juez, None)
            st, snap = pedir(a.plataforma, "/api/snapshot")
            if st == 200 and snap["estado"]["finalizado"]:
                time.sleep(8)
                print("[demo] partido finalizado; entrega exportada.", flush=True)
                break
        except Exception as e:
            print("[demo] esperando plataforma…", e, flush=True)
        time.sleep(0.5)


if __name__ == "__main__":
    main()
