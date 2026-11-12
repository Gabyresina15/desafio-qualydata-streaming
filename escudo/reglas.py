"""Motor de reglas: señal oficial, licenciatarios, riesgo colateral y armado de evidencia sellada."""
import base64
import binascii
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

from .util import hash_canonico, sha256_hex

IGNORED = "IGNORED_LEGITIMATE"
DOMAIN_ONLY = "DOMAIN_ONLY"
IP_AND_DOMAIN = "IP_AND_DOMAIN"


def raiz(host: str) -> str:
    partes = host.lower().rstrip(".").split(".")
    return ".".join(partes[-2:])


def pertenece(host: str, dominios: List[str]) -> bool:
    host = (host or "").lower().rstrip(".")
    return any(host == d or host.endswith("." + d) for d in dominios)


def _prefijo_coincide(marca: str, prefijo: str) -> bool:
    return marca == prefijo or marca.startswith(prefijo + "-")


def prefijos_partido(partido: dict) -> Tuple[List[str], List[str], List[str]]:
    """(prefijos oficiales, prefijos de licenciatarios, dominios autorizados)."""
    lic_pref = [l["prefijo_marca"] for l in partido.get("licenciatarios", [])]
    lic_dom = [d for l in partido.get("licenciatarios", []) for d in l.get("dominios_autorizados", [])]
    oficiales = [m["prefijo"] for m in partido.get("marcas_de_agua_validas", []) if m["prefijo"] not in lic_pref]
    return oficiales, lic_pref, lic_dom


def verificar_senal(det: dict, partido: dict) -> dict:
    """Filtro de señal y licenciatario. Devuelve {ok, codigo, motivo, prefijo_oficial}."""
    oficiales, lic_pref, lic_dom = prefijos_partido(partido)
    marca = (det.get("marca_de_agua_extraida") or "").strip()
    if not marca:
        return {"ok": False, "codigo": "SIN_MARCA_DE_AGUA", "motivo": "La captura no contiene marca de agua."}
    if any(_prefijo_coincide(marca, p) for p in lic_pref):
        return {"ok": False, "codigo": "MARCA_DE_LICENCIATARIO",
                "motivo": f"La marca {marca} corresponde a un licenciatario autorizado."}
    prefijo = next((p for p in oficiales if _prefijo_coincide(marca, p)), None)
    if prefijo is None:
        return {"ok": False, "codigo": "MARCA_AJENA_AL_PARTIDO",
                "motivo": f"La marca {marca} no corresponde a la señal oficial de este partido."}
    pagina_host = urlparse(det.get("pagina") or "").hostname or ""
    for h in (det.get("host_stream", ""), pagina_host):
        if h and pertenece(h, lic_dom):
            return {"ok": False, "codigo": "DOMINIO_DE_LICENCIATARIO",
                    "motivo": f"{h} es un dominio autorizado de un licenciatario."}
    return {"ok": True, "codigo": "SENAL_OFICIAL", "motivo": "Marca oficial verificada; el dominio no es de un licenciatario.",
            "prefijo_oficial": prefijo}


def clasificar_bloqueo(det: dict, partido: dict, dns_estado: int, dns: dict) -> dict:
    """Regla anti-daño colateral. IP_AND_DOMAIN solo si la IP es 100 % dedicada al stream pirata."""
    _, _, lic_dom = prefijos_partido(partido)
    host = det["host_stream"]
    if dns_estado != 200:
        return {"block_type": DOMAIN_ONLY, "riesgo_colateral": "DESCONOCIDO", "dominios_ajenos": [],
                "motivos": ["Sin registros de DNS pasivo: no se puede probar que la IP sea dedicada."]}
    dominios = dns.get("dominios", [])
    org = (dns.get("organizacion") or "").lower()
    motivos: List[str] = []
    ajenos = [d for d in dominios if raiz(d) != raiz(host) or pertenece(d, lic_dom)]
    if ajenos:
        motivos.append(f"La IP aloja {len(ajenos)} dominio(s) ajeno(s) al stream: {', '.join(ajenos[:4])}"
                       + ("…" if len(ajenos) > 4 else ""))
    if any(k in org for k in ("cdn", "compartid")):
        motivos.append(f"El ASN ({dns.get('organizacion')}) es CDN u hosting compartido.")
    if host not in dominios:
        motivos.append("El host del stream no figura en el DNS pasivo de esa IP.")
    if det.get("asn") is not None and dns.get("asn") is not None and det["asn"] != dns["asn"]:
        motivos.append("El ASN informado en la detección no coincide con el del DNS pasivo.")
    if motivos:
        return {"block_type": DOMAIN_ONLY, "riesgo_colateral": "ALTO", "dominios_ajenos": ajenos, "motivos": motivos}
    return {"block_type": IP_AND_DOMAIN, "riesgo_colateral": "BAJO", "dominios_ajenos": [],
            "motivos": [f"La IP aloja solo el stream pirata ({len(dominios)} dominio/s del mismo operador): bloqueo por IP habilitado."]}


def armar_evidencia(det: dict, verif: dict, dns_estado: int, dns: dict, partido: dict, minuto: float) -> dict:
    """Payload de evidencia (lo que se sella con SHA-256 canónico)."""
    cap = det.get("captura") or {}
    try:
        crudo = base64.b64decode(cap.get("contenido_b64", ""), validate=True)
        cap_ok = True
    except (binascii.Error, ValueError):
        crudo, cap_ok = b"", False
    ip = det["ip_servidor"]
    ahora = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    return {
        "version": 1,
        "partido_id": partido.get("id"),
        "deteccion_id": det["id"],
        "minuto_deteccion": det["minuto"],
        "timestamp_utc": ahora,
        "pagina": det.get("pagina"),
        "stream_url": det.get("stream_url"),
        "host_stream": det["host_stream"],
        "ip_servidor": ip,
        "asn": {"numero": det.get("asn"), "organizacion": dns.get("organizacion") if dns_estado == 200 else None},
        "marca_de_agua": {"extraida": det.get("marca_de_agua_extraida"),
                          "prefijo_oficial_coincidente": verif.get("prefijo_oficial"),
                          "coincide_con_partido": True, "es_licenciatario": False},
        "captura": {"formato": cap.get("formato"), "duracion_seg": cap.get("duracion_seg"),
                    "capturada_min": cap.get("capturada_min"), "bytes": len(crudo), "decodifica_ok": cap_ok,
                    "sha256": sha256_hex(crudo), "contenido_b64": cap.get("contenido_b64")},
        "dns_pasivo": {"consulta": f"/dns-pasivo?ip={ip}", "consultado_utc": ahora, "estado_http": dns_estado,
                       "dominios": dns.get("dominios", []) if dns_estado == 200 else [],
                       "asn": dns.get("asn") if dns_estado == 200 else None},
        "promocion": det.get("promocion", []),
    }


def sello(evidencia: dict) -> str:
    return hash_canonico(evidencia)


def sello_expediente(exp: dict) -> str:
    """Sello del expediente: ata evidencia + tipo de bloqueo + vencimiento + vínculo con el expediente madre."""
    return hash_canonico({"id": exp["id"], "evidence_hash": exp["evidence_hash"], "block_type": exp["block_type"],
                          "vence_min": exp["vence_min"], "parent_id": exp.get("parent_id"), "tipo": exp["tipo"]})


def verificar_expediente(exp: dict) -> Dict[str, bool]:
    return {"evidencia_integra": sello(exp["evidencia"]) == exp["evidence_hash"],
            "sello_integro": sello_expediente(exp) == exp["sello_expediente"]}
