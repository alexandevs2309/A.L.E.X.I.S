# ALEXIS — Estado de Implementación

## Actualmente

La v0.3 es una **Complete Foundation**, no una versión final.

### Implementado como fundamento
- contractos principales
- Mission Envelope
- estados de misión
- planner inicial
- policy engine
- event bus local (+ suscriptores sync/async)
- memoria en proceso
- learning/experience inicial
- agent registry
- tool registry
- recovery contract
- world model
- meta-cognition
- experiment contract
- perception interfaces
- communication interfaces
- model router interface
- execution placeholder
- verification placeholder
- API FastAPI (misiones reales, run/approve, SSE /stream, /ui) con token opcional `ALEXIS_API_TOKEN`
- demo web sin dependencias (apps/demo): **UI de experiencia conversacional** (sin orb/dashboard; contextos idle/thinking/researching/executing/waiting_approval/completed/error; decisiones de aprobación y cancelación; debug en «Modo Sistema»)
- Experience layer v0.4-m0: `alexis/experience/presenter.py` traduce el estado real del runtime a un modelo de presentación humano (ver `docs/EXPERIENCE-UI.md`)
- capa de storage PostgreSQL/pgvector (**A1**): esquema y repositorios de missions, mission_events y audit_log, verificados con persistencia/reinicio/recuperación
- storage v0.4 (esquema + repositorios): tasks, executions, observations, verifications, checkpoints + contratos `Task/TaskState/Execution/Checkpoint`, verificados con persistencia real de task/checkpoint
- Docker Compose con volumen de datos persistente y password via env
- `.dockerignore`, dev-deps (pytest) en `pyproject.toml`
- PostgreSQL/pgvector preparado
- NATS preparado (sin uso: los eventos durables serán en PostgreSQL, ver `docs/DESIGN-v0.4.md`)
- documentación arquitectónica (+ docs/DEVELOPMENT.md, docs/AUDIT-v0.3.md, docs/DESIGN-v0.4.md)
- **Avatar/Face Runtime v0.1** (`apps/face`, Vite+TS+Three.js): rostro 3D procedural con morph targets reales, face rig (Head/Neck/Jaw/Eyes/FaceRoot), gaze, blink natural, head motion, micro-motion, material neuronal GLSL, partículas, FaceBehaviorEngine (10 estados), `setAlexisState`/`CoreStateAdapter` (Core→rostro), **VOZ real por Web Speech API en español latino es-419** (narra resultados, pide decisión, saludo; `VoiceDriver` + boca a tiempo mientras suena), lip-sync por fonema reservado (honesto), DEBUG `?debug=1`. Rutas demo: `/` (Face App), `/classic`, `/avatar`, `/face/*`. Detalles y requisitos del GLB pendiente en `docs/AVATAR-FACE-RUNTIME.md` (`docs/AVATAR-FACE-AUDIT.md`).

### Próxima implementación real

**v0.4 — Real Autonomous Runtime** (diseño y decisiones en `docs/DESIGN-v0.4.md`)

1. ~~PostgreSQL persistence~~ — **hecho en A1**
2. ~~contratos v0.4 + tabla/repo tasks/executions/observations/verifications/checkpoints~~ — **hecho (S1)**
3. ~~runtime persistente (S2)~~ — **hecho y verificado**: la misión se persiste en cada transición; verificación y auditoría se escriben en PostgreSQL; recuperación de misiones abiertas al reiniciar (probado en vivo)
4. worker runtime + task leases/heartbeats (**S3 hecho**): `TaskRunner` con estados `queued→running→completed/failed/retrying`, lease (`lease_until`) + heartbeat, retry con `max_attempts=3`, checkpoint por `step_index`, `resume` desde el último checkpoint (verificado en vivo: aprobación reanuda en el paso pendiente sin re-ejecutar lo hecho); `Scheduler` reclamando leases huérfanas en el arranque (anti-huérfanas, deadline expirado → failed). Tasks/executions/checkpoints persistentes en PostgreSQL.
5. scheduler (**S3 hecho**, ver arriba: `alexis/autonomy/scheduler.py`)
6. checkpointing + recovery (**S3 hecho**): `CheckpointRepository` + `TaskRunner.save_checkpoint`/`resume`; el demo reanuda misiones abiertas al reiniciar desde el último checkpoint.
7. sandbox subprocess restringido (**S4 hecho**): `alexis/security/sandbox.py` — cwd acotado al workspace, env limpio, RLIMIT (AS/CPU/FSIZE/NOFILE/NPROC/CORE), timeout real, sin red (limitación de capa documentada). Probado: captura de `timeout` y de `MemoryError` por RLIMIT.
8. filesystem tools reales (**S4 hecho**): `fs.read`, `fs.stat`, **`fs.write`** y **`fs.remove`** registradas (`alexis/tools/filesystem.py`), ejecutadas en subprocess sandbox, con `schema/permissions/timeout/limits` en `Tool`. `SandboxExecutor` ejecuta pasos reales (analyze honesto; research→`fs.read`/`fs.stat` según intención; execute→`fs.remove`/`fs.write`/`fs.read` según intención; verify→`fs.stat`); paso sin tool → error explícito; `fs.remove` rechaza el workspace y directorios.
9. verificación independiente determinista (**S5 hecho**): `FilesystemVerifier` re-estadifica la ruta en el workspace sin confiar en el executor; para escritura exige que el archivo **exista ahora**, para borrado que **no exista** (evidencia real; no se afirma una tarea no hecha); objetivos sin ruta → rechazados.
10. política de permisos por intención (**hecho, regla actual M1**): `classify_objective_intent` (oráculo de intención compartido planner/executor/verifier; en v0.5 es *entrada* de la Policy Engine, no fuente única de permiso — ver `docs/AUTONOMY-V0.5-CAPABILITIES.md`) separa `read | write | destructive | unsupported`. **Regla actual determinista**: solo `destructive` (borrar/eliminar/sobreescribir) marca `requires_approval` y pausa una vez (`approved_step_ids`), reanudando desde el checkpoint. Leer y crear/editar corren automáticamente; renombrar/mover/copiar falla honesto (no soportado en M1).
11. ~~API/CLI con persistencia real + tests de integración (S6)~~ — **hecho en el demo**: persistencia real de tasks/executions/checkpoints/verifications; `/state` expone tasks + checkpoint_step + tools + workspace; 23 tests (12 previos + sandbox + runtime con tasks/checkpoint/scheduler + intención read/write/destructive/unsupported + verificación en vivo por HTTP).
12. terminal tool (post-M1)
13. Git/GitHub tool (post-M1)
14. real model adapters (post-M1, M1 es determinista)
15. durable memory (post-M1)
16. **Self Model F0-Self** (**implementado**, `docs/SELF-MODEL.md`): `alexis/self/`
    (modelo, presencia, sync) consume los eventos reales del bus y deriva las 18 zonas
    del autoconocimiento operacional (estado, capacidades, permisos, confianza,
    lecciones…); `/self` en el demo responde las auto-preguntas y expone `status`
    (`idle…error`). 9 tests nuevos; suite total 123 verde. **F0-Presence pendiente**:
    `presenter` y `CoreStateAdapter` pasarán a leer `self.status` (el frontend dejará
    de deducir estados).
17. **Capabilities F1** (**implementado**, `docs/AUTONOMY-V0.5-CAPABILITIES.md`):
    `alexis/capabilities/` (30 CapabilitySpec: 14 habilitadas, 16 `missing`),
    `alexis/security/policy_rules.py` (5 reglas como datos), `PolicyDecision` granular
    con `verdict`/`matched_rule`, planner por etapas con `step.capability`, tools
    vinculadas a `capability_id`/`sandbox_profile`, `GET /capabilities`. 7 tests nuevos;
    suite 130 verde.
18. **F2.0 — Contratos del Cognitive Core** (**implementado**,
    `docs/COGNITIVE-CORE-F2.md`): `alexis/cognition/contracts.py` (`IntentKind`,
    `Intent` con `is_task`/`is_direct_answer`, `SelfBrief.from_snapshot`, `Claim`/
    `ClaimKind` con `is_sound`, `CapabilityProposal` vs `Selection`, `ReplanDecision`,
    `UserReply` con `cognition_outcome`/`degraded`), `alexis/memory/contracts.py`
    (`MemoryQuery`/`MemoryItem`/`MemoryContext` con prompt de datos marcados) y
    extensiones retrocompatibles en `alexis/contracts.py`: `PlanStep`
    (`requires_input`/`verification`/`proposed_by`/`rationale`) y `Verification`
    (`checks`/`claim_ids`/`independent` + `VerificationCheck` + `verification_can_approve`).
    29 tests nuevos.
19. **F2.1 — Model layer** (**implementado**): `alexis/models/provider.py` (contrato
    `ModelProvider`, `ModelTask`, `ModelRequest`/`ModelResponse` con tri-estado P1
    `REAL|DEGRADED|UNAVAILABLE`), `alexis/models/router.py` (routing por tarea, privacy,
    presupuesto, deadline, cadena de fallback y evento `model.routed`),
    `alexis/models/degraded.py` (`DegradedProvider`/`EchoModel` marcados `[degraded]`),
    `alexis/models/providers/` (`local_http`, `openai_compatible`, `omniroute` adapter
    honesto `available=False` sin endpoint) y `alexis/models/config.py` (provider por
    entorno, sin editar el Core). 33 tests nuevos; suite total **192 verde**.

### Precisiones vinculantes de F2 (aprobadas)

- **P1** tri-estado de procedencia cognitiva: `REAL` (modelo real) / `DEGRADED`
  (contingencia determinista) / `UNAVAILABLE` (no hay provider). `DEGRADED` se
  propaga hasta la respuesta final y queda en `model.routed`; nunca se presenta como
  razonamiento real.
- **P2** separación estricta: el **Cognitive Core** decide *qué* hacer; el **Model
  Router** decide *qué proveedor* puede satisfacerlo. OmniRoute será un provider más,
  nunca el cerebro; el Core no depende de ningún provider concreto.
- **P3** invariantes: greeting y capability-query no crean misión; sólo
  `IntentKind.TASK` crea misión; Policy/Gates siguen siendo la autoridad; el modelo
  nunca se auto-otorga permisos; `ModelCritic` nunca aprueba solo; verificación
  independiente obligatoria; replan acotado; nunca éxito sin evidencia/verificación.

### Nomenclatura de fases

- Línea de inteligencia: `F0` Self Model · `F1` Capabilities foundation · `F2`
  Cognitive Core.
- Línea de capacidades: `C1` Capability Foundation (completado) · `C2` Web Research ·
  `C3` External Integrations · `C4` Advanced Capabilities · `C5` Environment/IoT.

## Criterio de avance

Una capacidad no se considera terminada porque exista una clase o interfaz. Se considera terminada cuando:

- ejecuta una operación real;
- respeta políticas;
- registra evidencia;
- puede fallar de forma controlada;
- puede recuperarse cuando corresponda;
- tiene pruebas;
- puede auditarse.
