# ALEXIS — Auditoría Técnica v0.3

**Fecha:** 2026-09-21
**Alcance:** repositorio completo (código, infraestructura, documentación, tests).
**Método:** inspección de código fuente, ejecución de tests, compilación de todo el árbol, análisis de uso cruzado de módulos, revisión de Docker/configuración. Nada fue implementado durante esta auditoría.

**Regla aplicada:** `CÓDIGO REAL > DOCUMENTACIÓN`. Cualquier capacidad listada abajo que no esté verificada operacionalmente se marca como tal.

---

## 1. Resumen ejecutivo

ALEXIS v0.3 es una **fundación arquitectónica honesta**: contratos sólidos, máquina de estados de misión, policy engine, bus de eventos local, capa de storage PostgreSQL/pgvector (A1) verificada contra una base real, API FastAPI funcional con aprobación humana y una interfaz que consume estado real vía SSE. 

**No hay todavía:** ejecución real de herramientas (el executor es simulado), persistencia de misión integrada al runtime, memoria durable, modelos conectados, verificación independiente real, scheduler, checkpoints, recovery, sandbox, defensa contra prompt injection en código, ni infraestructura NATS usada.

```
IMPLEMENTED      4 módulos        INTERFACE ONLY  2 módulos
PARTIAL          8 módulos        PLACEHOLDER      6 módulos
```

---

## 2. Verificaciones ejecutadas

| Verificación | Resultado |
|---|---|
| `py_compile` de todo el árbol (`alexis/**`, `apps/**`, `tests/**`) | OK |
| `pytest -q` (foundation + storage + integración con pg real) | **4 passed** |
| `python -m alexis.storage migrate && verify` contra PostgreSQL real (pgvector, puerto 5433) | OK: misión persistida→reinicio→recuperada con eventos y auditoría |
| Demo web stdlib (`apps/demo`) `GET /`, `GET /state`, `POST /missions`, `GET /stream` | OK (misión completa con 4 pasos, confianza real 0.70, 4 observaciones) |
| Uso cruzado de módulos (`grep` imports externos por dominio) | 8 dominios sin ningún consumidor |

---

## 3. Inventario técnico (estado real por módulo)

> Clasificación: **IMPLEMENTED** (verificado operativo) · **PARTIAL** (funciona pero incompleto) · **INTERFACE ONLY** · **PLACEHOLDER** (no realiza trabajo real) · **PLANNED** (solo documentado).

| Módulo | Estado | Notas |
|---|---|---|
| `alexis/contracts.py` | **PARTIAL** | Contratos base correctos. Faltan contratos de v0.4: `Task`, `TaskState`, `Checkpoint`, `ToolCall`, `Approval`, `Artifact`, estados `queued/retrying/cancelled`. |
| `alexis/core/runtime.py` | **PARTIAL** | Bucle lineal `plan→policy→execute→verify`. NO hay CONTEXT, EVALUATE, REPLAN, retry, recovery ni persistencia. Se detiene en `WAITING_APPROVAL` sin poder reanudar el mismo plan; `Fail → Analyzing → Replan` no existe. |
| `alexis/autonomy/mission.py` | **PARTIAL** | Solo `create`/`stop`. Sin scheduler, checkpoints, leases, heartbeats. |
| `alexis/autonomy/recovery.py` | **PLACEHOLDER** | Lógica trivial (`should_retry`/`next_action` fijos) **sin ningún consumidor**. |
| `alexis/cognition/planner.py` | **PARTIAL** | Plantilla fija de 4 pasos; no consume contexto ni modelos; nunca emite riesgo `HIGH/CRITICAL`. |
| `alexis/security/policy.py` | **PARTIAL** | Envelope funcional (permite/niega/aprueba). No hay defensa prompt injection, original..., sandbox ni RBAC. Autoriza implícitamente `analyze`/`verify` fuera del envelope. |
| `alexis/execution.py` | **PLACEHOLDER** | `LocalExecutor` DEVUELVE éxito siempre: **simulado**, sin herramienta real. |
| `alexis/verification.py` | **PLACEHOLDER** | `BasicVerifier` SIEMPRE devuelve `passed=True` confianza 0.70. No hay verificación independiente. |
| `alexis/events/bus.py` | **IMPLEMENTED (parcial vs directiva)** | Bus local con suscriptores sync/async verificado. **No durable**: NATS declarado pero sin uso. Suscriptores no se limpian (fuga de memoria en streams de larga duración). |
| `alexis/memory/store.py` | **PARTIAL** | `InMemoryMemory` funcional (4 observaciones verificadas). Sin pgvector, sin filtrado/confianza/versión. |
| `alexis/learning/system.py` | **PLACEHOLDER** | Solo acumula dicts en lista. No evalúa, no genera skills, no versiona. |
| `alexis/agents/registry.py` | **INTERFACE ONLY** | Diccionario estático de roles. Sin agentes ejecutables ni coordinación. |
| `alexis/tools/registry.py` | **IMPLEMENTED** | Registro funcional. **0 herramientas registradas** en toda la aplicación. |
| `alexis/meta/cognition.py` | **IMPLEMENTED** | Fórmula de confianza correcta. **Aislado**: ningún módulo lo consume. |
| `alexis/models/router.py` | **INTERFACE ONLY** | `complete()` lanza `NotImplementedError`. |
| `alexis/world/model.py` | **IMPLEMENTED** | CRUD de entidades. Lo consume solo el demo (entidad PostgreSQL pgvector, real). No conectado al runtime ni persistido. |
| `alexis/perception/interfaces.py` | **INTERFACE ONLY** | Protocols (vision/screen/audio/sensor). Sin implementación. |
| `alexis/communication/interfaces.py` | **INTERFACE ONLY** | Protocols STT/TTS/notificación. |
| `alexis/experiments/engine.py` | **PLACEHOLDER** | Devuelve el experimento sin análisis. Aislado. |
| `alexis/prediction/engine.py` | **PLACEHOLDER** | `detect_signals` devuelve `[]`. Aislado. |
| `alexis/observability/audit.py` | **PARTIAL** | `AuditLog` en memoria, NO conectado a runtime/API/storage. |
| `alexis/experience/state.py` | **PLACEHOLDER** | `ExperienceState` con 6 campos sin uso. |
| `alexis/storage/` (db, schema, repositories, serialization, `__main__`) | **IMPLEMENTED (A1)** | Verificado contra PG real: pool psycopg, DDL idempotente, `mission_events`, `audit_log`, extensión vector. **Faltan** tablas de v0.4: tasks, plans, plan_steps, executions, tool_calls, observations, verifications, memories, experiences, skills, approvals, checkpoints, artifacts. |
| `apps/api/main.py` | **PARTIAL** | `/health`, `/ui`, `/missions` (CRUD en memoria), `/run`, `/approve`, `/stream` (SSE) funcionales. Store en **memoria** (no persistido), sin auth, `create_task` sin esperar. **No ejecutable en este entorno** (fastapi no instalado). |
| `apps/demo/server.py` | **IMPLEMENTED** | Server stdlib verificado (UI + `/state` + `/stream` + misiones reales). |
| `apps/ui.py` | **IMPLEMENTED** | Interfaz consumidora de `/state`+`/stream` con 6 paneles de datos reales. |
| `tests/` | **PARTIAL** | 4 tests (foundation 2 + storage 2). Sin: runtime, policy edge, events, aprobación, API, recovery, tools, security. |

---

## 4. Qué funciona realmente (verificado, no inferido)

1. Contratos y máquina de estados de misión (`pending→planning→running→waiting_approval→completed/failed`).
2. Policy Engine: acciones fuera del envelope → `WAITING_APPROVAL` con `pending_approval` en contexto (flujo probado: ejecutar sin permiso → aprobar → completar).
3. Planner: genera 4 pasos con dependencias.
4. EventBus: historial + suscriptores sync/async; flujo de eventos completo de una misión (planning→steps→completed) capturado por SSE.
5. Storage A1: esquema, repositorios y CLI `migrate`/`verify`; **persistencia→reinicio→recuperación** probada contra `pgvector/pgvector:pg16` real con eventos y auditoría.
6. API FastAPI: endpoints definidos (pendiente probarse con fastapi instalado).
7. Demo stdlib + UI: paneles Misión/Cognición/Mundo/Agentes/Herramientas/Memoria con datos del runtime real (confianza 0.70 real, 10 agentes, entidad PostgreSQL registrada, 4 observaciones, feed de eventos).
8. Docker: imagen `pgvector/pgvector:pg16` y contenedor `postgres` corriendo; build de imagen python pendiente de probar.

---

## 5. Qué NO funciona / placeholders / interfaces solas

- **Ejecución real**: `LocalExecutor` es puramente simulado → el milestone "Coder modifica 2 archivos" NO existe.
- **Verificación**: `BasicVerifier` no verifica nada; siempre pasa.
- **Modelos**: `ModelRouter.complete` no implementado.
- **Recovery/checkpoints/scheduler/leases/heartbeats**: solo `RecoveryManager` trivial sin uso.
- **Prompt injection / sandbox / RBAC**: solo en `docs/SECURITY.md`.
- **Auditoría**: clase en memoria, no conectada.
- **Percepción/comunicación/experience/prediction/experiments/learning**: interfaces o placeholders aislados.
- **Persistencia de misión en el flujo real**: la API guarda en un dict en memoria; el runtime no persiste.

---

## 6. Inconsistencias código ↔ documentación / diseño

| # | Inconsistencia | Detalle |
|---|---|---|
| I1 | Puerto DSN | `docker-compose` expone `5433:5432`, pero `db.py` usa por defecto `localhost:5432` y `.env.example` `postgres:5432`. Correr el script local contra el compose exige forzar la URL a `:5433`. |
| I2 | Flujo de aprobación casi muerto en la API | La API añade siempre `execute` a `allowed_actions` y el planner marca `execute` como `MEDIUM` (autorizable sin aprobación). Las ramas `HIGH/CRITICAL→approval` del policy nunca se alcanzan con el planner actual. |
| I3 | Policy con acciones implícitas | `authorize()` permite `analyze`/`verify` aunque no estén en `allowed_actions`. La regla "solo envelope" no es estricta. Coincide con el test existente, pero contradice la rigidez que promete `SECURITY.md`/BLUEPRINT. |
| I4 | "Definido ≠ implementado" | `SYSTEM-MAP.md` lista 15 sistemas como "Definido" (correcto), pero `IMPLEMENTATION-STATUS.md` debe seguir marcado con precisión: la auditoría añade `storage A1`, `API real`, `demo/UI real`. |
| I5 | NATS | Declarado en compose y `.env`, **nunca importado/usado**. Decisión de arquitectura pendiente (ver §9). |
| I6 | pytest | `[tool.pytest.ini_options]` existe pero pytest/pytest-asyncio NO están en dependencias (ni dev). |
| I7 | Compose sin volumen | PostgreSQL sin `volumes`: `docker compose down` borra los datos ⚠️ (contradice "persistir estado crítico"). |
| I8 | Dockerfile sin bootstrap | `pip install .` correcto tras arreglar `pyproject` (build-system), pero el contenedor no ejecuta migraciones ni conoce la DB al arrancar. |
| I9 | Credenciales | `POSTGRES_PASSWORD=change-me` codificado; `.env.example` con password por defecto. |

---

## 7. Deuda técnica

1. Sin type-checking/linting configurado (mypy/ruff ausentes).
2. Sin logs estructurados; `print` usado en `__main__`.
3. Sin migraciones versionadas (pendiente A2).
4. Sin `.dockerignore` → la imagen puede incluir `__pycache__`, `.venv`, logs.
5. Sin CI ni medición de coverage.
6. `EventBus` sin lifecycle de suscriptores (fuga en SSE largos).
7. API dual (`apps/api` + `apps/demo`) compartiendo `apps/ui.PAGE`; hay que mantener sincronizados `/state`.
8. Tests frágiles: `test_policy_allows_analyze` usa `type()` para fabricar el `step` en vez de `PlanStep`.
9. `version 0.3.0` duplicada (pyproject, FastAPI, health).

---

## 8. Riesgos arquitectónicos

| Riesgo | Severidad | Mitigación | Estado |
|---|---|---|---|
| **R1** Ejecución simulada confundible con real | Alta | Criterio de "capacidad terminada"; marcar PLACEHOLDER hasta que una tool ejecute algo real con auditoría; prohibir "demo que lo finge". | abierto |
| **R2** Un solo proceso sin scheduler/recovery/task runtime → misiones largas no durables | Alta | v0.4: Task Runtime + checkpoints + recovery sobre storage (Milestone 1). | cerrado (S3) |
| **R3** Prompt injection sin defensa en código y con futuras tools de red/browser | Alta | **CERRADO**: filtro `datos ≠ instrucciones` en código (`alexis/security/untrusted.py`): marca `[[UNTRUSTED_DATA]]`, neutraliza metainstrucciones (EN/ES) y marcadores de rol, y rompe sus propios delimitadores. Aplicado en los dos puntos donde una observación llega a contexto/prompt (`SelfModel.active_context`, `MemoryContext.as_prompt_lines`). El envelope sólo lo muta el Core/Policy, nunca una observación. Tests: `tests/test_security_untrusted.py`. | **cerrado** |
| **R4** API sin auth puede quedar expuesta | Media | **CERRADO**: `ALEXIS_API_TOKEN` es **obligatorio en `production`** (el arranque falla, `apps/api/auth.py`); en `development` es opcional pero con **warning explícito** en el log. Comparación en tiempo constante (`secrets.compare_digest`). Los endpoints de misión y `/stream` exigen token; `/health` y `/ui` quedan abiertos a propósito. Bind `127.0.0.1` por defecto (`make run`, compose publica sólo en loopback). Tests: `tests/test_security_api_auth.py`. | **cerrado** |
| **R5** Postgres sin volumen = pérdida de datos | Alta | Volumen nombrado en compose. | cerrado |
| **R6** `change-me` y credenciales en compose | Media | **CERRADO**: `docker-compose.yml` usa `${VAR:?mensaje}` para `POSTGRES_PASSWORD` y `ALEXIS_API_TOKEN`: sin valor, `docker compose up` **falla** en vez de arrancar con `change-me`/vacío. Plantilla `.env.example`, `make env-check`, y flujo documentado en `docs/DEVELOPMENT.md §7`. | **cerrado** |
| **R7** NATS sin uso ni decisión | Media | Definir si hace falta ahora (ver D2). | abierto |
| **R8** FastAPI no probada en ejecución real aquí (env sin pip) | Media | Probada en ejecución real aquí (import + arranque en dev/production); sigue sin test de HTTP end-to-end. | parcial |

### 8.1 R10 — Credencial de terceros en texto plano (cerrado como proceso, pendiente como higiene)

`secrets/elevenlabs.env` contiene una `ALEXIS_ELEVENLABS_API_KEY` con formato real
(`sk_…`). El archivo está en `.gitignore` y no se versiona, pero **vive dentro del
directorio/ZIP del proyecto**. Mitigaciones añadidas:

- `secrets/*.env.example` con placeholders: el flujo de setup ya no depende de un
  archivo real dentro del repo.
- `scripts/check_secrets.py` (`make secrets-check`, hook de `pre-commit`): falla si
  cualquier archivo versionable trae una credencial con aspecto real y **nunca imprime
  el valor** (sólo ruta, línea y patrón).
- `.gitignore` afinado: `secrets/*` ignorado **excepto** `*.example`; `.env` ignorado
  excepto `.env.example`.

**Pendiente para el operador:** la key ya fue rotada fuera de este chat; esta copia del
proyecto aún la contiene, por lo que `make secrets-check` falla a propósito. Sustituir
el contenido real por la plantilla (`cp secrets/elevenlabs.env.example
secrets/elevenlabs.env`) y el check queda verde.

---

## 9. Dependencias

**Runtime del sistema:** Python ≥3.11 (3.12.3 en el entorno); este entorno carece de `pip`/`venv`/`sudo` → la API FastAPI no se puede ejecutar aquí; el demo stdlib sí.

**Librerías declaradas (pyproject):** `fastapi>=0.115`, `uvicorn[standard]>=0.30`, `pydantic>=2.8`, `psycopg[binary]>=3.1`, `psycopg_pool>=3.2`.

**Test:** `pytest`, `pytest-asyncio` (usados, no declarados → añadir a `[project.optional-dependencies] dev`).

**Infra:** Docker + imagen `pgvector/pgvector:pg16` (verificado), imagen `nats:2` (sin uso). Servicios corriendo en este entorno: postgres `:5433`, demo `:8100`.

**Cadena de dependencia para v0.4:** storage(A1 hecho) → misión persistida → task runtime sobre evento durable → scheduler/checkpoint/recovery → tools reales sobre sandbox → modelos → verificación independiente → auditoría persistida → CLI/API → tests de integración.

---

## 10. Prioridades (alineadas con la directiva)

No saltar a avatar/IoT/voz/RL. Prioridad: **una misión real de principio a fin**.

1. **Persistence Layer + Mission Persistence** — extender A1: tablas de v0.4 + migraciones (A2) + runtime que persiste la misión en cada transición.
2. **Event Infrastructure** — evento durable (recomendación: PostgreSQL outbox/listen-notify en single-node; NATS cuando haya multi-workers).
3. **Task Runtime / Worker / Scheduler / Checkpoints / Recovery** — leases, heartbeats, timeouts, retry, `queued/running/waiting_approval/completed/failed/cancelled/retrying`.
4. **Tool Runtime + Sandbox** — primero tools de bajo riesgo (filesystem read/write en workspace autorizado, luego git), crítico: **no comandos arbitrarios en el host**.
5. **Model Router** — adaptadores provider-agnósticos + determinístico primero.
6. **Memory** — episódica sobre pgvector, luego semántica/procedural/grafo.
7. **Verification independiente + Audit persistido + CLI/API**.
8. **Integration/security/recovery tests**.

---

## Anexo — comandos de verificación usados

```bash
python3 -m py_compile alexis/*.py alexis/*/*.py apps/*.py apps/api/*.py apps/demo/*.py tests/*.py
docker run --rm --network host -e ALEXIS_DATABASE_URL=postgresql://alexis:change-me@127.0.0.1:5433/alexis \
  -e PYTHONPATH=/app -v "$PWD":/app -w /app python:3.12-slim bash -lc \
  "pip install -q psycopg[binary] psycopg_pool pytest pytest-asyncio && pytest -q"
python3 -m alexis.storage migrate && python3 -m alexis.storage verify   # contra PG real
python3 -m apps.demo.server                                             # http://127.0.0.1:8100
```