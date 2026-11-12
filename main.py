#!/usr/bin/env python3
"""Arranque de la plataforma: conecta con el simulador y sirve la UI del juez."""
import argparse
import sys
import threading
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
sys.path.insert(0, str(RAIZ))

from escudo.exportar import exportar  # noqa: E402
from escudo.plataforma import Plataforma  # noqa: E402
from escudo.web import crear_servidor  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description="Escudo Live: plataforma federal de bloqueo en vivo")
    ap.add_argument("--simulador", default="http://localhost:8080")
    ap.add_argument("--puerto", type=int, default=8090)
    ap.add_argument("--juzgados", type=Path, default=RAIZ / "config" / "juzgados.json")
    ap.add_argument("--usuarios", type=Path, default=RAIZ / "config" / "usuarios_demo.json")
    ap.add_argument("--salida", type=Path, default=RAIZ / "salida")
    ap.add_argument("--entrega", type=Path, default=RAIZ / "entrega")
    ap.add_argument("--salir-al-exportar", action="store_true", help="termina el proceso tras la exportación final")
    ap.add_argument("--solo-ip-en-dedicadas", action="store_true", help="en IPs dedicadas envía solo IP a ISPs compatibles")
    ap.add_argument("--demo-forzar-ip", action="store_true", help="solo demo/video: fuerza 1 bloqueo IP en IP compartida para probar reclamos")
    a = ap.parse_args()

    plat = Plataforma(a.simulador, a.juzgados, a.usuarios, a.salida, a.entrega,
                      a.salir_al_exportar, a.solo_ip_en_dedicadas, a.demo_forzar_ip)
    plat.exportador = exportar
    srv = crear_servidor(plat, a.puerto)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"UI del juez: http://localhost:{a.puerto}  |  simulador: {a.simulador}", flush=True)
    plat.iniciar()
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        plat.detener()
        exportar(plat, "interrupcion")


if __name__ == "__main__":
    main()
