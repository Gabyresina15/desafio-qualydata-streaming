"""Exportación de la entrega: expedientes, auditoría verificable, métricas y propuesta normativa."""
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from . import reglas
from .ledger import verificar_cadena

DOCS = Path(__file__).resolve().parent.parent / "docs"


def _limpio(d: dict) -> dict:
    return {k: v for k, v in d.items() if not k.startswith("_")}


def exportar(plat, motivo: str) -> None:
    out: Path = plat.entrega
    out.mkdir(parents=True, exist_ok=True)
    with plat.lock:
        expedientes = []
        for exp in plat.expedientes.values():
            e = _limpio(exp)
            e["slots"] = [dict(s) for s in exp["slots"].values()]
            e["ordenes_isp"] = [
                {k: r[k] for k in ("pid", "oid", "tipo", "objetivo", "estado", "aplicada_min", "juez_id")}
                for r in plat.ordenes.values() if r["exp_id"] == exp["id"]]
            e["verificacion_integridad"] = reglas.verificar_expediente(exp)
            mins = [s["aplicada_min"] for s in exp["slots"].values() if s.get("aplicada_min") is not None]
            e["metricas"] = {"minuto_deteccion": exp["minuto_deteccion"],
                             "minutos_hasta_confirmacion_en_todos_los_isp":
                                 None if not mins or exp["confirmado_todos_min"] is None
                                 else round(exp["confirmado_todos_min"] - exp["minuto_deteccion"], 3)}
            expedientes.append(e)
        reclamos = [_limpio({**r, "ordenes": [list(o) for o in r["ordenes"]]}) for r in plat.reclamos.values()]
        entradas = plat.ledger.copia()
    ok, seq, msg = verificar_cadena(entradas)
    (out / "expedientes.json").write_text(json.dumps(expedientes, ensure_ascii=False, indent=2), encoding="utf-8")
    ejemplo = next((e for e in expedientes if e["estado"] in ("EXPIRED", "ACTIVE") and e["block_type"] == "DOMAIN_ONLY"), None) \
        or next((e for e in expedientes if e["estado"] in ("EXPIRED", "ACTIVE")), None) or (expedientes[0] if expedientes else {})
    (out / "expediente_ejemplo.json").write_text(json.dumps(ejemplo, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "reclamos.json").write_text(json.dumps(reclamos, ensure_ascii=False, indent=2), encoding="utf-8")
    with open(out / "auditoria.jsonl", "w", encoding="utf-8") as f:
        for e in entradas:
            f.write(json.dumps(e, ensure_ascii=False, sort_keys=True) + "\n")
    (out / "auditoria_verificacion.json").write_text(json.dumps(
        {"integra": ok, "seq_corrupto": seq, "detalle": msg, "entradas": len(entradas),
         "hash_cabeza": entradas[-1]["currentHash"] if entradas else None, "exportado_utc":
         datetime.now(timezone.utc).isoformat(timespec="seconds"), "motivo_exportacion": motivo},
        ensure_ascii=False, indent=2), encoding="utf-8")
    por_tipo = {}
    for e in entradas:
        por_tipo[e["tipo"]] = por_tipo.get(e["tipo"], 0) + 1
    lat = [e["metricas"]["minutos_hasta_confirmacion_en_todos_los_isp"] for e in expedientes
           if e["metricas"]["minutos_hasta_confirmacion_en_todos_los_isp"] is not None]
    (out / "resumen.json").write_text(json.dumps({
        "expedientes": len(expedientes), "eventos_por_tipo": por_tipo,
        "latencia_deteccion_a_confirmacion_total_min": {"promedio": round(sum(lat) / len(lat), 3) if lat else None,
                                                        "maxima": max(lat) if lat else None},
        "bloqueos_ip_vigentes_al_final": sum(1 for r in plat.ordenes.values() if r["tipo"] == "ip" and r["estado"] in ("recibida", "aplicada")),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    origen = DOCS / "propuesta_normativa.md"
    if origen.exists():
        shutil.copyfile(origen, out / "propuesta_normativa.md")
