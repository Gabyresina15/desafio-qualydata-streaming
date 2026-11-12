# Escudo Live — Plataforma Federal de Órdenes de Bloqueo en Vivo

Solución para el track **Datos y Streaming (Desafío Fútbol)** de **Hackathon Argentina Space 2026**.

## Arquitectura y Reglas de Ingeniería

1. **Motor de Reglas y Anti-Daño Colateral (`escudo/reglas.py`)**:
   - **Filtro de Señal y Licenciatario**: Verifica la marca de agua extraída contra `GET /partido` y descarta cualquier detección cuya marca corresponda a un licenciatario autorizado, a otro partido o a su dominio autorizado, registrándolo en auditoría como `IGNORED_LEGITIMATE`.
   - **DNS Pasivo y Riesgo Colateral**: Consulta `GET /dns-pasivo?ip=<ip>`. Si la IP aloja otros dominios ajenos al stream, pertenece a una CDN o hosting compartido, o no tiene registros DNS pasivos, clasifica el expediente obligatoriamente como `DOMAIN_ONLY`. Solo habilita `IP_AND_DOMAIN` cuando la IP es 100 % dedicada al stream pirata.
   - **Evidencia Criptográfica**: Calcula el hash SHA-256 canónico (`evidence_hash`) sobre captura, marca de agua, IP, ASN/ISP, consulta DNS pasivo y timestamp UTC, además de un `sello_expediente` que vincula el payload con el tipo de bloqueo, vencimiento y expediente madre.

2. **Gestión de Turnos Judiciales en Tiempo Real (`escudo/plataforma.py`)**:
   - Sincroniza permanentemente con `GET /estado` y `GET /juzgados`.
   - Cada juez inicia sesión con su usuario y contraseña de demostración (`config/usuarios_demo.json`, verificada con PBKDF2-SHA256) y solo puede firmar mientras está de turno.
   - Ante un cambio de turno, revoca inmediatamente la sesión del juez saliente y reasigna los expedientes `PENDING_SIGNATURE` y reclamos `ABIERTO` al juez entrante.
   - Incluye modo **One-Click / Teclas rápidas** (`Enter`, `R`, `L`, `↑`/`↓`) y **Autodespacho asistido** activable por el juez durante su turno.

3. **Despachador Multi-ISP con Reintentos Asíncronos (`escudo/plataforma.py`)**:
   - Adapta el payload según `capacidades` de cada proveedor (si solo soporta `dominio`, envía solo dominio aunque el expediente habilite IP).
   - Despacho paralelo no bloqueante (`ThreadPoolExecutor`) con **Exponential Backoff + Jitter** (`200 ms → 500 ms → 1 s`) y cobertura paralela ante proveedores inestables que fallan aleatoriamente.
   - Detecta rotaciones de dominio/IP por marca de agua y las vincula al expediente madre (`ACTUALIZACION_ROTACION`).

4. **Módulo de Reclamos y Vencimiento Automático**:
   - Escucha `GET /reclamos` y también re-chequea periódicamente el DNS pasivo de las IPs bloqueadas.
   - Si un sitio legítimo resulta afectado, presenta una alerta prioritaria al juez de turno para firmar el levantamiento de la orden por IP (`POST .../levantar`) y despachar en el mismo acto un reemplazo `DOMAIN_ONLY`.
   - Fija `vence_min` al final del partido en cada orden firmada y al finalizar el partido ejecuta el control y levantamiento de respaldo si quedara algún bloqueo por IP vigente.

5. **Auditoría Inmutable y Exportación (`escudo/ledger.py`, `escudo/exportar.py`)**:
   - Registro append-only `salida/auditoria.jsonl` encadenado con `prevHash` y `currentHash` (SHA-256).
   - Exportación automática al finalizar el partido y al cerrar la ventana de gracia hacia `entrega/`: `expedientes.json`, `expediente_ejemplo.json`, `reclamos.json`, `auditoria.jsonl`, `auditoria_verificacion.json`, `resumen.json` y `propuesta_normativa.md`.

## Requisitos y Dependencias

- **Python 3.9+** (solo biblioteca estándar, sin dependencias externas).
- **Código preexistente**: únicamente el simulador y evaluador oficial de [`desafio-futbol`](https://github.com/qualydata-org/desafio-futbol). Todo el código de `escudo-live` fue desarrollado durante el hackathon.

## Cómo Correrlo

1. **Iniciar el simulador** (en la carpeta `desafio-futbol`):
   ```bash
   python simulador/servidor.py --escenario datos/escenario_practica.json --acelerar 6
   ```
2. **Iniciar Escudo Live** (en la carpeta `escudo-live`):
   ```bash
   python main.py --simulador http://localhost:8080 --puerto 8090 --juzgados ../desafio-futbol/datos/juzgados.json
   ```
   - Por defecto el autodespacho está **bloqueado** (`POST /api/auto` responde `403`): en el partido final **todas las órdenes se firman a mano desde la UI**.
   - `--permitir-autodespacho`: habilita el interruptor de autodespacho únicamente para pruebas automatizadas o prácticas locales.
   - `--solo-ip-en-dedicadas`: opcional; en IPs dedicadas envía solo `ip` a ISPs compatibles (por defecto envía `ip` y `dominio`).
   - `--demo-forzar-ip`: solo para grabar la demo de reclamos en el video (fuerza 1 bloqueo por IP en una IP compartida para que el simulador dispare un reclamo real y mostrar el levantamiento firmado + reemplazo por dominio).
3. **Abrir la UI del juez** en `http://localhost:8090` e ingresar con un usuario demo (`jueza-laura-ficticia` / `laura-demo-2026` o `juez-martin-ejemplo` / `martin-demo-2026`). La sesión se guarda por pestaña (`sessionStorage`), así que podés dejar abiertas dos pestañas simultáneas (una por cada juez de turno). En el partido final la aprobación se realiza **manualmente desde la UI** (un clic o tecla `Enter`, con notificación sonora y visual ante cada expediente o reclamo).
   - *(Solo para pruebas automatizadas de práctica con `--permitir-autodespacho`)*:
     ```bash
     python herramientas/jueces_demo.py
     ```
4. **Correr los tests**:
   ```bash
   python tests/test_core.py
   python tests/test_integracion.py
   ```
5. **Evaluar la corrida y la entrega** (exporta `expedientes.json`, `expediente_ejemplo.json`, `reclamos.json`, `auditoria.jsonl`, `auditoria_verificacion.json`, `transparencia.json`, `resumen.json` y `propuesta_normativa.md`):
   ```bash
   python evaluacion/verificar.py --log salida/registro.jsonl
   python herramientas/verificar_entrega.py entrega
   python herramientas/demo_alteracion.py entrega
   ```

## Supuestos

- Las claves de firma (`config/juzgados.json`) y usuarios de demostración (`config/usuarios_demo.json`) son los ficticios provistos para el simulador.
- Si el simulador en el partido final corre en otra URL o puerto, se indica con `--simulador <URL>`.
- Si el partido final incorpora nuevos jueces en `GET /juzgados`, sus claves de firma y credenciales deben figurar en los archivos de configuración correspondientes (o pasarse con `--juzgados` / `--usuarios`).
