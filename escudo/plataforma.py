"""Plataforma federal de órdenes de bloqueo en vivo: motor central.

Hilos: detecciones, turnos judiciales, monitor de órdenes (confirmaciones, reintentos, vencimiento),
reclamos. Los envíos a proveedores van por un pool paralelo no bloqueante.
"""
import hashlib
import json
import random
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import reglas
from .ledger import Ledger
from .simclient import RelojPartido, SimClient
from .util import hash_canonico, hmac_hex, mensaje_levantar, mensaje_orden

BACKOFF = (0.2, 0.5, 1.0)        # segundos; el último se repite
JITTER_MAX = 0.05                # jitter corto
MARGEN_TURNO = 0.05              # min de partido: no se firma pegado al fin del turno
HEDGE_PROVEEDOR_INESTABLE = 2    # órdenes paralelas hacia un proveedor que ya falló
ESTADOS_VIVOS = ("recibida", "aplicada")
SLOT_HECHO = ("CONFIRMADO", "VENCIDO", "HEREDADO")
SLOT_MUERTO = ("LEVANTADO", "CANCELADO")


class Error(Exception):
    def __init__(self, codigo: int, mensaje: str):
        super().__init__(mensaje)
        self.codigo, self.mensaje = codigo, mensaje


def _pbkdf2(pwd: str, sal: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", pwd.encode(), sal, 60_000).hex()


class Plataforma:
    def __init__(self, base: str, juzgados_path: Path, usuarios_path: Path, salida: Path, entrega: Path,
                 salir_al_exportar: bool = False, solo_ip_en_dedicadas: bool = False,
                 demo_forzar_ip: bool = False):
        self.sim = SimClient(base)
        self.reloj = RelojPartido()
        self.salida, self.entrega = Path(salida), Path(entrega)
        self.ledger = Ledger(self.salida / "auditoria.jsonl")
        self.lock = threading.RLock()
        self.pool = ThreadPoolExecutor(max_workers=48)
        self.activo = True
        self.salir_al_exportar = salir_al_exportar
        self.solo_ip_en_dedicadas = solo_ip_en_dedicadas
        self.demo_forzar_ip = demo_forzar_ip
        self._demo_ip_forzada: Optional[str] = None

        datos = json.loads(Path(juzgados_path).read_text(encoding="utf-8"))
        usuarios = json.loads(Path(usuarios_path).read_text(encoding="utf-8"))["usuarios"]
        self.jueces: Dict[str, dict] = {}
        for jz in datos["juzgados"]:
            for j in jz["jueces"]:
                sal = secrets.token_bytes(16)
                self.jueces[j["id"]] = {"id": j["id"], "nombre": j["nombre"], "juzgado_id": jz["id"],
                                        "juzgado": jz["nombre"], "clave": j["clave_firma_demo"], "sal": sal,
                                        "pwd": _pbkdf2(usuarios.get(j["id"], secrets.token_hex(8)), sal)}
        self.turnos: List[dict] = list(datos["turnos"])
        self.sim_duty: Optional[set] = None
        self.duty_prev: Optional[frozenset] = None

        self.partido: dict = {}
        self.proveedores: List[dict] = []
        self.sesiones: Dict[str, dict] = {}
        self.expedientes: Dict[str, dict] = {}
        self.por_clave: Dict[Tuple[str, str], str] = {}
        self.por_marca: Dict[str, str] = {}
        self.piratas: set = set()
        self.ordenes: Dict[Tuple[str, str], dict] = {}
        self.reclamos: Dict[str, dict] = {}
        self.dns_vistos: Dict[str, set] = {}
        self.fallos_isp: Dict[str, int] = {}
        self.contador_exp = 0
        self.contador_rc = 0
        self.fin_iniciado = False
        self.fin_t0 = 0.0
        self.alerta_fin = False
        self.export_1 = False
        self.export_2 = False
        self.exportador = None  # se inyecta desde main

    # ------------------------------------------------------------------ utilidades
    def log(self, evento: str, /, **datos: Any) -> dict:
        return self.ledger.agregar(evento, datos, self.reloj.ahora())

    def _hilo(self, fn, nombre: str, pausa: float) -> None:
        def ciclo():
            while self.activo:
                try:
                    fn()
                except Exception as e:  # el hilo nunca debe morir
                    try:
                        self.log("ERROR_INTERNO", hilo=nombre, error=repr(e))
                    except Exception:
                        pass
                time.sleep(pausa)
        threading.Thread(target=ciclo, name=nombre, daemon=True).start()

    def iniciar(self) -> None:
        while True:  # esperar al simulador
            try:
                st, estado = self.sim.get("/estado")
                if st == 200:
                    self.reloj.sincronizar(estado)
                    break
            except Exception:
                pass
            time.sleep(0.5)
        _, self.partido = self.sim.get("/partido")
        _, self.proveedores = self.sim.get("/proveedores")
        self.log("PLATAFORMA_INICIADA", partido=self.partido.get("id"), fin_min=self.partido.get("fin_min"),
                 proveedores=[{"id": p["id"], "capacidades": p["capacidades"]} for p in self.proveedores],
                 jueces_de_turno=estado.get("jueces_de_turno"))
        self._tick_turnos()
        self._hilo(self._tick_turnos, "turnos", 0.15)
        self._hilo(self._tick_detecciones, "detecciones", 0.15)
        self._hilo(self._tick_ordenes, "ordenes", 0.12)
        self._hilo(self._tick_reclamos, "reclamos", 0.3)
        self._hilo(self._tick_dns_recheck, "dns-recheck", 2.0)

    # ------------------------------------------------------------------ turnos
    def en_turno(self, juez_id: str, ahora: Optional[float] = None) -> bool:
        ahora = self.reloj.ahora() if ahora is None else ahora
        local = any(t["juez_id"] == juez_id and t["desde_min"] <= ahora < t["hasta_min"] - MARGEN_TURNO
                    for t in self.turnos)
        return local and (self.sim_duty is None or juez_id in self.sim_duty)

    def jueces_en_turno(self) -> List[str]:
        a = self.reloj.ahora()
        return [j for j in self.jueces if self.en_turno(j, a)]

    def elegir_juez(self) -> Optional[str]:
        activos = self.jueces_en_turno()
        if activos:
            return activos[0]
        a = self.reloj.ahora()
        futuros = sorted((t for t in self.turnos if t["desde_min"] > a and t["juez_id"] in self.jueces),
                         key=lambda t: t["desde_min"])
        return futuros[0]["juez_id"] if futuros else None

    def sesion_activa(self, juez_id: str) -> List[dict]:
        return [s for s in self.sesiones.values() if s["juez_id"] == juez_id and not s["revocada"]]

    def puede_firmar(self, juez_id: Optional[str]) -> bool:
        return bool(juez_id) and self.en_turno(juez_id) and bool(self.sesion_activa(juez_id))

    def auto_activo(self, juez_id: str) -> bool:
        return self.puede_firmar(juez_id) and any(s["auto"] for s in self.sesion_activa(juez_id))

    def _tick_turnos(self) -> None:
        st, estado = self.sim.get("/estado")
        if st != 200:
            return
        self.reloj.sincronizar(estado)
        self.sim_duty = set(estado.get("jueces_de_turno", []))
        if int(time.monotonic()) % 3 == 0:  # refresco periódico de turnos publicados
            s2, jz = self.sim.get("/juzgados")
            if s2 == 200 and jz.get("turnos"):
                self.turnos = jz["turnos"]
        duty = frozenset(self.jueces_en_turno())
        with self.lock:
            if self.duty_prev is None:
                self.duty_prev = duty
                self.log("TURNO_INICIAL", jueces_de_turno=sorted(duty))
                self._reasignar_pendientes()
                return
            if duty == self.duty_prev:
                return
            salientes = sorted(self.duty_prev - duty)
            entrantes = sorted(duty - self.duty_prev)
            self.duty_prev = duty
            self.log("CAMBIO_DE_TURNO", salientes=salientes, entrantes=entrantes, de_turno_ahora=sorted(duty))
            for s in self.sesiones.values():  # invalida capacidad de firma del saliente
                if s["juez_id"] in salientes and not s["revocada"]:
                    s["revocada"] = True
                    self.log("SESION_REVOCADA", juez_id=s["juez_id"], motivo="fin de turno")
            self._reasignar_pendientes()

    def _reasignar_pendientes(self) -> None:
        destino = self.elegir_juez()
        if not destino:
            return
        for exp in self.expedientes.values():
            if exp["estado"] == "PENDING_SIGNATURE" and exp["juez_asignado"] != destino:
                previo, exp["juez_asignado"] = exp["juez_asignado"], destino
                self.log("REASIGNACION_EXPEDIENTE", expediente=exp["id"], de=previo, a=destino, motivo="cambio de turno")
        for rc in self.reclamos.values():
            if rc["estado"] == "ABIERTO" and rc["juez_asignado"] != destino:
                previo, rc["juez_asignado"] = rc["juez_asignado"], destino
                self.log("REASIGNACION_RECLAMO", reclamo=rc["id"], de=previo, a=destino, motivo="cambio de turno")
        self._auto_pendientes()

    def _auto_pendientes(self) -> None:
        for exp in list(self.expedientes.values()):
            if exp["estado"] == "PENDING_SIGNATURE" and self.auto_activo(exp["juez_asignado"]):
                self._firmar_interno(exp, exp["juez_asignado"], "aprobar", "AUTO_POLICY")
        for rc in list(self.reclamos.values()):
            if rc["estado"] == "ABIERTO" and self.auto_activo(rc["juez_asignado"]):
                self._resolver_reclamo(rc, rc["juez_asignado"], "levantar", "AUTO_POLICY")

    # ------------------------------------------------------------------ sesiones
    def login(self, juez_id: str, password: str) -> dict:
        j = self.jueces.get(juez_id)
        if not j or not secrets.compare_digest(_pbkdf2(password or "", j["sal"]), j["pwd"]):
            raise Error(401, "Usuario o contraseña incorrectos")
        a = self.reloj.ahora()
        if not any(t["juez_id"] == juez_id and t["hasta_min"] > a for t in self.turnos):
            raise Error(403, "Tu turno ya terminó: no podés ingresar")
        token = secrets.token_urlsafe(24)
        with self.lock:
            self.sesiones[token] = {"token": token, "juez_id": juez_id, "auto": False, "revocada": False,
                                    "t": time.time()}
            self.log("LOGIN_JUEZ", juez_id=juez_id, en_turno=self.en_turno(juez_id))
        return {"token": token, "juez": self._vista_juez(juez_id)}

    def sesion(self, token: Optional[str]) -> dict:
        s = self.sesiones.get(token or "")
        if not s:
            raise Error(401, "Sesión inexistente")
        if s["revocada"]:
            raise Error(401, "Tu credencial de turno fue invalidada (fin de turno)")
        return s

    def logout(self, token: str) -> None:
        s = self.sesiones.get(token)
        if s and not s["revocada"]:
            s["revocada"] = True
            self.log("LOGOUT_JUEZ", juez_id=s["juez_id"])

    def set_auto(self, token: str, activar: bool) -> dict:
        s = self.sesion(token)
        with self.lock:
            s["auto"] = bool(activar)
            self.log("AUTODESPACHO_" + ("ACTIVADO" if activar else "DESACTIVADO"), juez_id=s["juez_id"],
                     alcance="expedientes verificados por el motor de reglas y reclamos validados por DNS pasivo",
                     vigencia="solo durante el turno del juez")
            self._auto_pendientes()
        return {"auto": s["auto"]}

    def _vista_juez(self, juez_id: str) -> dict:
        j = self.jueces[juez_id]
        return {"id": j["id"], "nombre": j["nombre"], "juzgado": j["juzgado"], "juzgado_id": j["juzgado_id"],
                "en_turno": self.en_turno(juez_id)}

    # ------------------------------------------------------------------ detecciones y expedientes
    def _tick_detecciones(self) -> None:
        desde = getattr(self, "_desde", -1.0)
        st, lote = self.sim.get(f"/detecciones?desde={desde}")
        if st != 200:
            return
        for det in lote["detecciones"]:
            self.procesar_deteccion(det)
        self._desde = lote["minuto_actual"]

    def procesar_deteccion(self, det: dict) -> None:
        minuto = self.reloj.ahora()
        self.log("DETECCION", id=det["id"], minuto=det["minuto"], host=det["host_stream"], ip=det["ip_servidor"],
                 marca=det.get("marca_de_agua_extraida"), pagina=det.get("pagina"))
        if self.reloj.terminado():
            self.log("DETECCION_FUERA_DE_VENTANA", id=det["id"], motivo="el partido ya terminó")
            return
        v = reglas.verificar_senal(det, self.partido)
        if not v["ok"]:
            self.log(reglas.IGNORED, id=det["id"], host=det["host_stream"], ip=det["ip_servidor"],
                     codigo=v["codigo"], motivo=v["motivo"])
            return
        clave = (det["host_stream"], det["ip_servidor"])
        with self.lock:
            existente = self.por_clave.get(clave)
            if existente:
                self.expedientes[existente]["redetecciones"].append({"deteccion_id": det["id"], "minuto": det["minuto"]})
                self.log("REDETECCION", id=det["id"], expediente=existente,
                         estado_expediente=self.expedientes[existente]["estado"])
                return
        st, dns = self.sim.get(f"/dns-pasivo?ip={det['ip_servidor']}")
        clasif = reglas.clasificar_bloqueo(det, self.partido, st, dns)
        if self.demo_forzar_ip and not self._demo_ip_forzada and clasif["block_type"] == reglas.DOMAIN_ONLY and clasif["dominios_ajenos"]:
            self._demo_ip_forzada = det["ip_servidor"]
            clasif = {**clasif, "block_type": reglas.IP_AND_DOMAIN,
                      "motivos": clasif["motivos"] + ["[MODO DEMO RECLAMO] Bloqueo IP forzado para probar flujo de reclamos."]}
        evid = reglas.armar_evidencia(det, v, st, dns, self.partido, minuto)
        with self.lock:
            marca = det["marca_de_agua_extraida"]
            padre = self.por_marca.get(marca)
            exp = self._crear_exp(det, evid, clasif, "ACTUALIZACION_ROTACION" if padre else "ORIGINAL", padre, minuto)
            self.por_clave[clave] = exp["id"]
            self.por_marca.setdefault(marca, exp["id"])
            if st == 200:
                self.piratas.update(d for d in dns["dominios"] if reglas.raiz(d) == reglas.raiz(det["host_stream"]))
            self.piratas.add(det["host_stream"])
            if padre:
                p = self.expedientes[padre]
                p["hijos"].append(exp["id"])
                p["actualizaciones"].append({"tipo": "ROTACION_DE_SERVIDOR", "expediente": exp["id"],
                                             "nuevo_host": det["host_stream"], "nueva_ip": det["ip_servidor"]})
                self.log("EXPEDIENTE_ACTUALIZADO", expediente_madre=padre, nuevo_expediente=exp["id"],
                         motivo="el sitio rotó de dominio/IP", host=det["host_stream"], ip=det["ip_servidor"])
            self._asignar(exp)

    def _crear_exp(self, det: dict, evid: dict, clasif: dict, tipo: str, padre: Optional[str], minuto: float) -> dict:
        self.contador_exp += 1
        exp = {"id": f"EXP-2026-{self.contador_exp:06d}", "tipo": tipo, "parent_id": padre,
               "estado": "PENDING_SIGNATURE", "block_type": clasif["block_type"], "analisis_colateral": clasif,
               "evidencia": evid, "evidence_hash": reglas.sello(evid),
               "vence_min": int(self.partido["fin_min"]), "minuto_deteccion": det["minuto"], "creado_min": round(minuto, 3),
               "juez_asignado": None, "firmante": None, "firmado_via": None, "firma_decision": None,
               "ratificacion": False, "slots": {}, "hijos": [], "redetecciones": [], "actualizaciones": [],
               "confirmado_todos_min": None, "_det": det}
        exp["sello_expediente"] = reglas.sello_expediente(exp)
        self.expedientes[exp["id"]] = exp
        self.log("EXPEDIENTE_CREADO", expediente=exp["id"], tipo=tipo, parent_id=padre, deteccion=det["id"],
                 block_type=exp["block_type"], riesgo_colateral=clasif["riesgo_colateral"], motivos=clasif["motivos"],
                 host=det["host_stream"], ip=det["ip_servidor"], marca=det["marca_de_agua_extraida"],
                 evidence_hash=exp["evidence_hash"], sello_expediente=exp["sello_expediente"],
                 vence_min=exp["vence_min"])
        return exp

    def _asignar(self, exp: dict) -> None:
        exp["juez_asignado"] = self.elegir_juez()
        self.log("ASIGNACION_JUEZ", expediente=exp["id"], juez_id=exp["juez_asignado"])
        if self.auto_activo(exp["juez_asignado"] or ""):
            self._firmar_interno(exp, exp["juez_asignado"], "aprobar", "AUTO_POLICY")

    # ------------------------------------------------------------------ firma judicial
    def firmar_expediente(self, token: str, exp_id: str, decision: str) -> dict:
        s = self.sesion(token)
        with self.lock:
            exp = self.expedientes.get(exp_id)
            if not exp:
                raise Error(404, "Expediente inexistente")
            if exp["estado"] != "PENDING_SIGNATURE":
                raise Error(409, f"El expediente no está pendiente (estado {exp['estado']})")
            if exp["juez_asignado"] != s["juez_id"]:
                raise Error(403, "Este expediente no está en tu bandeja")
            if not self.puede_firmar(s["juez_id"]):
                raise Error(403, "Estás fuera de turno: no podés firmar")
            self._firmar_interno(exp, s["juez_id"], decision, "MANUAL_ONE_CLICK")
            return {"estado": exp["estado"]}

    def _firmar_interno(self, exp: dict, juez_id: str, decision: str, via: str) -> None:
        with self.lock:
            if exp["estado"] != "PENDING_SIGNATURE":
                return
            j = self.jueces[juez_id]
            firma = hmac_hex(j["clave"], f"DECISION|{exp['id']}|{exp['evidence_hash']}|{decision}|{juez_id}")
            if decision == "rechazar":
                exp["estado"] = "REJECTED"
                self.log("FIRMA_JUDICIAL", expediente=exp["id"], decision="RECHAZADO", juez_id=juez_id, via=via,
                         firma_decision=firma)
                return
            exp["firmante"], exp["firmado_via"], exp["firma_decision"] = juez_id, via, firma
            exp["estado"] = "DISPATCHING"
            self.log("FIRMA_JUDICIAL", expediente=exp["id"], decision="APROBADO", juez_id=juez_id, via=via,
                     firma_decision=firma, ratificacion=exp["ratificacion"], vence_min=exp["vence_min"],
                     latencia_desde_deteccion_min=round(self.reloj.ahora() - exp["minuto_deteccion"], 3))
            self._activar(exp)

    # ------------------------------------------------------------------ despachador multi-ISP
    def _objetivos(self, exp: dict, capacidades: Optional[List[str]] = None) -> List[Tuple[str, str]]:
        ev = exp["evidencia"]
        if exp["block_type"] == reglas.IP_AND_DOMAIN:
            if self.solo_ip_en_dedicadas:
                if capacidades is None or "ip" in capacidades:
                    return [("ip", ev["ip_servidor"])]
                return [("dominio", ev["host_stream"])]
            return [("ip", ev["ip_servidor"]), ("dominio", ev["host_stream"])]
        return [("dominio", ev["host_stream"])]

    def _activar(self, exp: dict) -> None:
        """Crea los slots (proveedor x tipo) la primera vez y despacha los pendientes, en paralelo."""
        with self.lock:
            heredar_de = self.expedientes.get(exp.get("_heredar", ""))
            if not exp["slots"]:
                for p in self.proveedores:
                    for tipo, objetivo in self._objetivos(exp, p["capacidades"]):
                        if tipo not in p["capacidades"]:
                            self.log("ADAPTACION_PAYLOAD", expediente=exp["id"], proveedor=p["id"], solicitado=tipo,
                                     enviado="dominio", motivo="el proveedor solo acepta bloqueo por dominio")
                            continue
                        key = f"{p['id']}:{tipo}"
                        slot = {"key": key, "pid": p["id"], "tipo": tipo, "objetivo": objetivo, "estado": "NUEVO",
                                "seq": 0, "reintentos": 0, "ordenes": [], "aplicada_min": None}
                        padre_slot = heredar_de["slots"].get(key) if heredar_de else None
                        if tipo == "dominio" and padre_slot and padre_slot["estado"] not in SLOT_MUERTO + ("VENCIDO",):
                            slot.update(estado="HEREDADO", heredado_de=heredar_de["id"],
                                        aplicada_min=padre_slot.get("aplicada_min"))
                        exp["slots"][key] = slot
            pendientes = [k for k, s in exp["slots"].items() if s["estado"] in ("NUEVO", "PENDING_SIGN")]
            for k in pendientes:
                exp["slots"][k]["estado"] = "ENVIANDO"
        for k in pendientes:
            self.pool.submit(self._enviar_slot, exp["id"], k)
        self._chequear_exp(exp)

    def _enviar_slot(self, exp_id: str, key: str) -> None:
        with self.lock:
            exp = self.expedientes[exp_id]
            slot = exp["slots"][key]
            if slot["estado"] in SLOT_HECHO + SLOT_MUERTO or exp["estado"] in ("REJECTED", "EXPIRED", "SUPERSEDED"):
                return
            if self.reloj.terminado():
                slot["estado"] = "CANCELADO"
                return
            if not self.puede_firmar(exp["firmante"]):
                if not self._ratificar(exp, slot):
                    return
            copias = HEDGE_PROVEEDOR_INESTABLE if self.fallos_isp.get(slot["pid"], 0) > 0 else 1
        for i in range(copias):
            if i == 0:
                self._post_orden(exp_id, key, "inicial" if slot["seq"] == 0 else "reintento")
            else:
                self.pool.submit(self._post_orden, exp_id, key, "cobertura_paralela_isp_inestable")

    def _ratificar(self, exp: dict, slot: dict) -> bool:
        """El juez firmante ya no está de turno: el envío debe ser ratificado por quien está de turno."""
        cand = next((j for j in self.jueces_en_turno() if self.sesion_activa(j)), None)
        if cand and self.auto_activo(cand):
            exp["firmante"], exp["firmado_via"] = cand, "AUTO_POLICY"
            self.log("RATIFICACION_AUTOMATICA", expediente=exp["id"], juez_id=cand,
                     motivo="reenvío tras cambio de turno: el firmante original ya no puede firmar")
            return True
        slot["estado"] = "PENDING_SIGN"
        if exp["estado"] != "PENDING_SIGNATURE":
            exp["estado"] = "PENDING_SIGNATURE"
            exp["ratificacion"] = True
            exp["juez_asignado"] = self.elegir_juez()
            self.log("RATIFICACION_REQUERIDA", expediente=exp["id"], juez_asignado=exp["juez_asignado"],
                     motivo="envío pendiente tras cambio de turno")
        return False

    def _post_orden(self, exp_id: str, key: str, motivo: str) -> None:
        with self.lock:
            exp = self.expedientes[exp_id]
            slot = exp["slots"][key]
            if slot["estado"] in SLOT_HECHO + SLOT_MUERTO:
                return
            slot["seq"] += 1
            oid = f"{exp_id}-{slot['pid']}-{slot['tipo'][0]}{slot['seq']}"
            j = self.jueces[exp["firmante"]]
            body = {"orden_id": oid, "expediente_id": exp_id, "tipo": slot["tipo"], "objetivo": slot["objetivo"],
                    "vence_min": exp["vence_min"], "juzgado_id": j["juzgado_id"], "juez_id": j["id"]}
            body["firma"] = hmac_hex(j["clave"], mensaje_orden(body))
            rec = {"pid": slot["pid"], "oid": oid, "exp_id": exp_id, "slot": key, "tipo": slot["tipo"],
                   "objetivo": slot["objetivo"], "estado": "enviando", "aplicada_min": None, "t_envio": time.time(),
                   "juez_id": j["id"]}
            self.ordenes[(slot["pid"], oid)] = rec
            slot["ordenes"].append(oid)
            self.log("ENVIO_ISP", expediente=exp_id, proveedor=slot["pid"], orden_id=oid, tipo=slot["tipo"],
                     objetivo=slot["objetivo"], vence_min=exp["vence_min"], juez_id=j["id"], motivo=motivo)
        try:
            st, resp = self.sim.post(f"/proveedores/{slot['pid']}/ordenes", body)
        except Exception as e:
            st, resp = 0, {"error": repr(e)}
        with self.lock:
            if st == 202:
                rec["estado"] = "recibida"
                return
            rec["estado"] = "rechazada"
            self.log("RECHAZO_ISP", expediente=exp_id, proveedor=slot["pid"], orden_id=oid, http=st,
                     motivo=resp.get("error") if isinstance(resp, dict) else str(resp))
            if st == 0 or st >= 500 or st == 409:
                self._programar_reintento(exp_id, key, f"HTTP {st}")
            else:
                slot["estado"] = "CANCELADO"

    def _programar_reintento(self, exp_id: str, key: str, motivo: str) -> None:
        with self.lock:
            slot = self.expedientes[exp_id]["slots"][key]
            if slot["estado"] in SLOT_HECHO + SLOT_MUERTO:
                return
            if any(self.ordenes[(slot["pid"], o)]["estado"] in ("enviando", "recibida", "aplicada") for o in slot["ordenes"]):
                return  # hay otra orden en vuelo (cobertura paralela)
            i = slot["reintentos"]
            slot["reintentos"] += 1
            demora = BACKOFF[min(i, len(BACKOFF) - 1)] + random.uniform(0, JITTER_MAX)
            self.log("REINTENTO_ISP", expediente=exp_id, proveedor=slot["pid"], intento=i + 1,
                     espera_ms=round(demora * 1000), motivo=motivo)
        t = threading.Timer(demora, lambda: self.pool.submit(self._enviar_slot, exp_id, key))
        t.daemon = True
        t.start()

    # ------------------------------------------------------------------ monitor de órdenes
    def _tick_ordenes(self) -> None:
        with self.lock:
            pids = {pid for (pid, _), r in self.ordenes.items() if r["estado"] in ESTADOS_VIVOS}
        listas = list(self.pool.map(lambda p: (p, self.sim.get(f"/proveedores/{p}/ordenes")), pids))
        for pid, (st, lista) in listas:
            if st != 200:
                continue
            with self.lock:
                for o in lista:
                    rec = self.ordenes.get((pid, o["orden_id"]))
                    if rec:
                        self._transicion(rec, o)
        if self.reloj.terminado():
            self._fin_partido()
        with self.lock:
            if (not self.export_2 and self.reloj.sincronizado
                    and self.reloj.ahora() >= self.reloj.fin_min + 5 and self.fin_iniciado):
                self.export_2 = True
                self._exportar("ventana_cerrada")

    def _transicion(self, rec: dict, o: dict) -> None:
        est = o["estado"]
        if est == rec["estado"] or est == "recibida":
            return
        exp = self.expedientes[rec["exp_id"]]
        slot = exp["slots"][rec["slot"]]
        base = {"expediente": exp["id"], "proveedor": rec["pid"], "orden_id": rec["oid"], "tipo": rec["tipo"],
                "objetivo": rec["objetivo"]}
        if est == "aplicada":
            rec["estado"], rec["aplicada_min"] = "aplicada", o.get("aplicada_min")
            if slot["estado"] not in SLOT_HECHO + SLOT_MUERTO:
                slot["estado"], slot["aplicada_min"] = "CONFIRMADO", o.get("aplicada_min")
            self.log("CONFIRMACION_ISP", aplicada_min=o.get("aplicada_min"),
                     latencia_deteccion_a_confirmacion_min=round((o.get("aplicada_min") or 0) - exp["minuto_deteccion"], 3),
                     **base)
            self._chequear_exp(exp)
        elif est == "fallida":
            rec["estado"] = "fallida"
            self.fallos_isp[rec["pid"]] = self.fallos_isp.get(rec["pid"], 0) + 1
            self.log("FALLO_ISP", motivo=o.get("motivo"), **base)
            if exp["estado"] not in ("REJECTED", "EXPIRED"):
                self._programar_reintento(exp["id"], rec["slot"], "orden_fallida")
        elif est in ("vencida", "levantada"):
            rec["estado"] = est
            if est == "vencida":
                if slot["estado"] == "CONFIRMADO":
                    slot["estado"] = "VENCIDO"
                self.log("VENCIMIENTO_ISP", finalizada_min=o.get("finalizada_min"),
                         unblock_ip=(rec["tipo"] == "ip"), **base)
            else:
                if slot["estado"] not in ("LEVANTADO",):
                    slot["estado"] = "LEVANTADO"
                self.log("LEVANTAMIENTO_CONFIRMADO_ISP", finalizada_min=o.get("finalizada_min"), **base)

    def _chequear_exp(self, exp: dict) -> None:
        if exp["estado"] != "DISPATCHING":
            return
        slots = [s for s in exp["slots"].values() if s["estado"] not in SLOT_MUERTO]
        if slots and all(s["estado"] in SLOT_HECHO for s in slots):
            exp["estado"] = "ACTIVE"
            mins = [s["aplicada_min"] for s in slots if s["aplicada_min"] is not None]
            exp["confirmado_todos_min"] = max(mins) if mins else None
            self.log("EXPEDIENTE_ACTIVO", expediente=exp["id"], confirmaciones=len(slots),
                     confirmado_todos_min=exp["confirmado_todos_min"],
                     latencia_total_min=None if not mins else round(max(mins) - exp["minuto_deteccion"], 3))

    # ------------------------------------------------------------------ vencimiento automático
    def _fin_partido(self) -> None:
        with self.lock:
            if not self.fin_iniciado:
                self.fin_iniciado, self.fin_t0 = True, time.time()
                self.log("FIN_PARTIDO", minuto_fin=self.reloj.fin_min)
                for exp in self.expedientes.values():
                    if exp["estado"] == "PENDING_SIGNATURE":
                        exp["estado"] = "EXPIRED"
                        self.log("VENCIMIENTO_EXPEDIENTE", expediente=exp["id"], motivo="partido terminado sin firma")
                for rc in self.reclamos.values():
                    if rc["estado"] == "ABIERTO":
                        rc["estado"] = "CADUCADO"
                vigentes = [{"proveedor": r["pid"], "orden_id": r["oid"], "objetivo": r["objetivo"]}
                            for r in self.ordenes.values() if r["tipo"] == "ip" and r["estado"] in ESTADOS_VIVOS]
                self.log("CRON_VENCIMIENTO_INICIADO", bloqueos_ip_vigentes=vigentes,
                         mecanismo="vence_min firmado en cada orden; verificación y levantamiento de respaldo")
            vivas = [r for r in self.ordenes.values() if r["estado"] in ESTADOS_VIVOS]
            ip_vivas = [r for r in vivas if r["tipo"] == "ip"]
            if ip_vivas and time.time() - self.fin_t0 > 3.0:
                self._levantar_respaldo(ip_vivas)
            if not vivas and not self.export_1:
                self.export_1 = True
                for exp in self.expedientes.values():
                    if exp["estado"] in ("ACTIVE", "DISPATCHING"):
                        exp["estado"] = "EXPIRED"
                self.log("CRON_VENCIMIENTO_COMPLETO", ordenes_ip_restantes=0)
                self._exportar("fin_de_partido")

    def _levantar_respaldo(self, ip_vivas: List[dict]) -> None:
        """Si un proveedor no venció solo una orden por IP, se levanta con el juez de turno (si tiene sesión y autodespacho)."""
        juez = next((j for j in self.jueces_en_turno() if self.auto_activo(j)), None)
        if not juez:
            if not self.alerta_fin:
                self.alerta_fin = True
                self.log("ALERTA_VENCIMIENTO_PENDIENTE", ordenes=[r["oid"] for r in ip_vivas],
                         motivo="requiere firma de levantamiento del juez de turno")
            return
        for r in ip_vivas:
            self.pool.submit(self._post_levantar, r["pid"], r["oid"], juez, "caducidad: levantamiento de respaldo")

    def _exportar(self, motivo: str) -> None:
        if self.exportador:
            try:
                self.exportador(self, motivo)
            except Exception as e:
                self.log("ERROR_INTERNO", hilo="exportar", error=repr(e))
        if self.salir_al_exportar and motivo == "ventana_cerrada":
            threading.Timer(1.0, lambda: __import__("os")._exit(0)).start()

    # ------------------------------------------------------------------ reclamos
    def _tick_reclamos(self) -> None:
        st, lote = self.sim.get("/reclamos?desde=-1")  # siempre todos; se deduplica por id
        if st != 200:
            return
        for r in lote["reclamos"]:
            with self.lock:
                if r["id"] not in self.reclamos:
                    self._abrir_reclamo(r["id"], r["dominio"], r["proveedor"], r.get("mensaje", ""), r["minuto"], "ISP_FEED")

    def _tick_dns_recheck(self) -> None:
        """Vigila las IP bloqueadas: si pasan a alojar sitios ajenos, abre un reclamo de oficio."""
        with self.lock:
            ips = {r["objetivo"] for r in self.ordenes.values() if r["tipo"] == "ip" and r["estado"] in ESTADOS_VIVOS}
        for ip in ips:
            if ip == self._demo_ip_forzada:
                continue
            st, d = self.sim.get(f"/dns-pasivo?ip={ip}")
            if st != 200:
                continue
            with self.lock:
                vistos = self.dns_vistos.setdefault(ip, set())
                nuevos = [x for x in d["dominios"] if x not in self.piratas and x not in vistos]
                vistos.update(d["dominios"])
                for dom in nuevos:
                    self.contador_rc += 1
                    self._abrir_reclamo(f"RC-{self.contador_rc:03d}", dom, "(oficio)", f"Re-chequeo DNS: {ip} ahora aloja {dom}",
                                        self.reloj.ahora(), "DNS_RECHEQUEO")

    def _afectadas_por(self, dom: str) -> List[dict]:
        _, _, lic_dom = reglas.prefijos_partido(self.partido)
        res = []
        cache: Dict[str, List[str]] = {}
        for r in self.ordenes.values():
            if r["estado"] not in ESTADOS_VIVOS:
                continue
            if r["tipo"] == "dominio" and r["objetivo"] == dom:
                res.append(r)
            elif r["tipo"] == "ip":
                if r["objetivo"] not in cache:
                    st, d = self.sim.get(f"/dns-pasivo?ip={r['objetivo']}")
                    cache[r["objetivo"]] = d.get("dominios", []) if st == 200 else []
                if dom in cache[r["objetivo"]]:
                    res.append(r)
        return res

    def _abrir_reclamo(self, rid: str, dom: str, pid: str, mensaje: str, minuto: float, origen: str) -> None:
        _, _, lic_dom = reglas.prefijos_partido(self.partido)
        self.log("RECLAMO_RECIBIDO", reclamo=rid, dominio=dom, proveedor=pid, mensaje=mensaje, origen=origen,
                 minuto_reclamo=minuto)
        legitimo = reglas.pertenece(dom, lic_dom) or dom not in self.piratas
        afectadas = self._afectadas_por(dom) if legitimo else []
        rc = {"id": rid, "origen": origen, "dominio": dom, "proveedor": pid, "mensaje": mensaje, "minuto": minuto,
              "estado": "ABIERTO", "prioridad": "ALTA", "juez_asignado": None, "ordenes": [(r["pid"], r["oid"]) for r in afectadas],
              "expedientes": sorted({r["exp_id"] for r in afectadas if r["tipo"] == "ip"}), "resolucion": None,
              "accion": ""}
        if not afectadas:
            rc["estado"] = "DESESTIMADO"
            self.reclamos[rid] = rc
            self.log("RECLAMO_DESESTIMADO", reclamo=rid, dominio=dom,
                     motivo="el dominio es del stream pirata o no hay un bloqueo vigente que lo afecte")
            return
        rc["accion"] = ("Levantar el bloqueo por IP en todos los proveedores y reemplazarlo por bloqueo por dominio "
                        "del stream pirata" if rc["expedientes"] else "Levantar el bloqueo del dominio legítimo")
        rc["juez_asignado"] = self.elegir_juez()
        self.reclamos[rid] = rc
        self.log("RECLAMO_VALIDADO", reclamo=rid, dominio=dom, ordenes_afectadas=[o for _, o in rc["ordenes"]],
                 expedientes=rc["expedientes"], juez_asignado=rc["juez_asignado"], prioridad="ALTA", accion=rc["accion"])
        if self.auto_activo(rc["juez_asignado"] or ""):
            self._resolver_reclamo(rc, rc["juez_asignado"], "levantar", "AUTO_POLICY")

    def resolver_reclamo(self, token: str, rid: str, decision: str) -> dict:
        s = self.sesion(token)
        with self.lock:
            rc = self.reclamos.get(rid)
            if not rc:
                raise Error(404, "Reclamo inexistente")
            if rc["estado"] != "ABIERTO":
                raise Error(409, f"El reclamo ya fue resuelto ({rc['estado']})")
            if rc["juez_asignado"] != s["juez_id"]:
                raise Error(403, "Este reclamo no está en tu bandeja")
            if not self.puede_firmar(s["juez_id"]):
                raise Error(403, "Estás fuera de turno: no podés firmar")
            self._resolver_reclamo(rc, s["juez_id"], decision, "MANUAL_ONE_CLICK")
            return {"estado": rc["estado"]}

    def _resolver_reclamo(self, rc: dict, juez_id: str, decision: str, via: str) -> None:
        with self.lock:
            if rc["estado"] != "ABIERTO":
                return
            if decision != "levantar":
                rc["estado"] = "RECHAZADO"
                self.log("RECLAMO_RECHAZADO_POR_JUEZ", reclamo=rc["id"], juez_id=juez_id, via=via)
                return
            rc["estado"] = "RESUELTO"
            vivas = [self.ordenes[k] for k in rc["ordenes"] if self.ordenes[k]["estado"] in ESTADOS_VIVOS]
            # alcance: toda orden por IP sobre las IP afectadas, en todos los proveedores
            ips = {r["objetivo"] for r in vivas if r["tipo"] == "ip"}
            lista = {(r["pid"], r["oid"]) for r in vivas}
            for r in self.ordenes.values():
                if r["tipo"] == "ip" and r["objetivo"] in ips and r["estado"] in ESTADOS_VIVOS:
                    lista.add((r["pid"], r["oid"]))
            hijos = []
            for eid in rc["expedientes"]:  # 1) primero el bloqueo fino de reemplazo (en paralelo)
                hijos.append(self._crear_reemplazo(self.expedientes[eid], rc, juez_id, via))
            self.log("FIRMA_JUDICIAL_RECLAMO", reclamo=rc["id"], juez_id=juez_id, via=via, decision="LEVANTAR_Y_REEMPLAZAR",
                     ordenes_a_levantar=sorted(o for _, o in lista), expedientes_reemplazo=hijos)
            rc["resolucion"] = {"juez_id": juez_id, "via": via, "reemplazos": hijos}
            for pid, oid in lista:  # 2) levantamientos firmados
                self.pool.submit(self._post_levantar, pid, oid, juez_id, f"reclamo {rc['id']}: {rc['dominio']}")

    def _post_levantar(self, pid: str, oid: str, juez_id: str, motivo: str) -> None:
        j = self.jueces[juez_id]
        body = {"juez_id": juez_id, "firma": hmac_hex(j["clave"], mensaje_levantar(oid, juez_id)), "motivo": motivo}
        self.log("LEVANTAMIENTO_ENVIADO", proveedor=pid, orden_id=oid, juez_id=juez_id, motivo=motivo)
        try:
            st, resp = self.sim.post(f"/proveedores/{pid}/ordenes/{oid}/levantar", body)
        except Exception as e:
            st, resp = 0, {"error": repr(e)}
        if st != 200:
            self.log("LEVANTAMIENTO_RECHAZADO", proveedor=pid, orden_id=oid, http=st, error=str(resp)[:200])
            if st == 0 or st >= 500:
                time.sleep(0.3)
                self.pool.submit(self._post_levantar, pid, oid, juez_id, motivo)

    def _crear_reemplazo(self, padre: dict, rc: dict, juez_id: str, via: str) -> str:
        det = padre["_det"]
        st, dns = self.sim.get(f"/dns-pasivo?ip={det['ip_servidor']}")
        v = reglas.verificar_senal(det, self.partido)
        clasif = {"block_type": reglas.DOMAIN_ONLY, "riesgo_colateral": "ALTO", "dominios_ajenos": [],
                  "motivos": [f"Reemplazo por reclamo {rc['id']} ({rc['dominio']}): la IP pasó a ser compartida; "
                              "se bloquea solo el dominio del stream."]}
        evid = reglas.armar_evidencia(det, v, st, dns, self.partido, self.reloj.ahora())
        exp = self._crear_exp(det, evid, clasif, "REEMPLAZO_RECLAMO", padre["id"], self.reloj.ahora())
        exp["minuto_deteccion"] = rc["minuto"]  # la latencia del reemplazo se mide desde el reclamo
        padre["hijos"].append(exp["id"])
        padre["block_type_vigente"] = reglas.DOMAIN_ONLY
        padre["actualizaciones"].append({"tipo": "IP_LEVANTADA_POR_RECLAMO", "reclamo": rc["id"], "expediente": exp["id"]})
        exp["juez_asignado"] = juez_id
        exp["estado"] = "PENDING_SIGNATURE"
        exp["_heredar"] = padre["id"]
        self._firmar_interno(exp, juez_id, "aprobar", via)
        # el slot IP del padre queda levantado; el reemplazo hereda las órdenes por dominio ya vigentes
        for s in padre["slots"].values():
            if s["tipo"] == "ip":
                s["estado"] = "LEVANTADO"
        return exp["id"]

    # ------------------------------------------------------------------ vistas
    def vista_exp(self, exp: dict, detalle: bool = False) -> dict:
        ev = exp["evidencia"]
        v = {"id": exp["id"], "tipo": exp["tipo"], "parent_id": exp["parent_id"], "hijos": exp["hijos"],
             "estado": exp["estado"], "block_type": exp["block_type"],
             "block_type_vigente": exp.get("block_type_vigente", exp["block_type"]),
             "riesgo_colateral": exp["analisis_colateral"]["riesgo_colateral"],
             "motivos": exp["analisis_colateral"]["motivos"], "dominios_ajenos": exp["analisis_colateral"]["dominios_ajenos"],
             "host": ev["host_stream"], "ip": ev["ip_servidor"], "asn": ev["asn"], "marca": ev["marca_de_agua"]["extraida"],
             "pagina": ev["pagina"], "minuto_deteccion": exp["minuto_deteccion"], "evidence_hash": exp["evidence_hash"],
             "dominios_en_ip": ev["dns_pasivo"]["dominios"], "captura_sha256": ev["captura"]["sha256"],
             "promocion": ev["promocion"], "juez_asignado": exp["juez_asignado"], "firmante": exp["firmante"],
             "firmado_via": exp["firmado_via"], "ratificacion": exp["ratificacion"], "vence_min": exp["vence_min"],
             "confirmado_todos_min": exp["confirmado_todos_min"], "redetecciones": len(exp["redetecciones"]),
             "slots": [{k: s[k] for k in ("pid", "tipo", "estado", "aplicada_min", "reintentos")} for s in exp["slots"].values()],
             "actualizaciones": exp["actualizaciones"]}
        if detalle:
            v["evidencia"] = ev
        return v

    def snapshot(self, token: Optional[str], desde_seq: int) -> dict:
        ses = self.sesiones.get(token or "")
        with self.lock:
            yo = None
            if ses:
                yo = dict(self._vista_juez(ses["juez_id"]), auto=ses["auto"], revocada=ses["revocada"])
            exps = list(self.expedientes.values())
            visibles = [e for e in exps if ses and (e["juez_asignado"] == ses["juez_id"] or e["firmante"] == ses["juez_id"])]
            rcs = [r for r in self.reclamos.values() if ses and r["juez_asignado"] == ses["juez_id"]]
            a = self.reloj.ahora()
            sig = [t for t in self.turnos if t["hasta_min"] > a]
            return {
                "estado": {"minuto": round(a, 2), "fin_min": self.reloj.fin_min, "finalizado": self.reloj.terminado(),
                           "acelerar": self.reloj.acelerar, "jueces_de_turno": self.jueces_en_turno()},
                "turnos": self.turnos,
                "jueces": {j["id"]: {"nombre": j["nombre"], "juzgado": j["juzgado"]} for j in self.jueces.values()},
                "yo": yo,
                "expedientes": [self.vista_exp(e) for e in visibles],
                "reclamos": [{k: v for k, v in r.items() if k != "ordenes"} for r in rcs],
                "stats": self._stats(exps),
                "proveedores": [{"id": p["id"], "nombre": p["nombre"], "capacidades": p["capacidades"]} for p in self.proveedores],
                "auditoria": self.ledger.copia(desde_seq)[-200:],
                "auditoria_total": len(self.ledger.entradas),
            }

    def _stats(self, exps: List[dict]) -> dict:
        c: Dict[str, int] = {}
        for e in exps:
            c[e["estado"]] = c.get(e["estado"], 0) + 1
        ign = sum(1 for e in self.ledger.copia() if e["tipo"] == reglas.IGNORED)
        return {"expedientes": len(exps), "por_estado": c, "ignorados": ign,
                "reintentos": sum(1 for e in self.ledger.copia() if e["tipo"] == "REINTENTO_ISP")}

    def transparencia(self) -> List[dict]:
        out = []
        with self.lock:
            for r in self.ordenes.values():
                if r["estado"] in ("rechazada", "enviando"):
                    continue
                exp = self.expedientes[r["exp_id"]]
                out.append({"expediente": r["exp_id"], "proveedor": r["pid"], "tipo": r["tipo"], "objetivo": r["objetivo"],
                            "estado": r["estado"], "aplicada_min": r["aplicada_min"], "hasta_min": exp["vence_min"],
                            "juez": self.jueces[r["juez_id"]]["nombre"]})
        return out

    def detener(self) -> None:
        self.activo = False
