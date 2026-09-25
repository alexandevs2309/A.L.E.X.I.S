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

20. **F2.1b — Cognitive Runtime mínimo (Fase 1 de la reorientación)** (**implementado**,
    `ALEXIS_COGNITIVE=1`): el runtime deja de recorrer el plan una vez y pasa a
    **decidir antes de cada acción y volver a decidir después de observar**.
    - `alexis/cognition/state.py`: `NextAction` (EXECUTE_TOOL, RESEARCH, ASK_USER, WAIT,
      REPLAN, VERIFY, FINISH, ABORT), `KnowledgeState` (known/unknown/hypotheses/
      assumptions/uncertainties + claims + huella de progreso) y `Decision` (acción +
      justificación + capability + claims + procedencia).
    - `alexis/cognition/evidence.py`: `EvidenceStore` + `ClaimGuard`. Observación →
      EVIDENCE; verificación determinista → FACT; modelo → INFERENCE/ASSUMPTION/
      UNCERTAINTY. **Un claim del modelo nunca es FACT**: `ClaimGuard` lo degrada y deja
      constancia en `degradations`.
    - `alexis/cognition/loop.py`: `CognitiveRuntime.step()` = decide → policy → execute →
      observe → evaluate. El modelo elige entre las opciones que el runtime calculó
      (`ModelTask.REASON`); si se sale de ellas, o si la respuesta no es real
      (DEGRADED/UNAVAILABLE), decide el suelo determinista y lo declara. Cotas:
      `max_replans=2`, `max_stalls=2` (detección de ausencia de progreso),
      `max_iterations=12`. Sin salida: ASK_USER o ABORT, nunca éxito sin verificación.
    - `alexis/core/runtime.py`: `AlexisRuntime.cognitive` como colaborador opcional; el
      camino legacy (`for step in plan`) queda intacto y comparte el epílogo `_finalize`.
      `MissionState.WAITING_CLARIFICATION` para ASK_USER. `SandboxExecutor.execute`
      acepta `tool_name=` (override de replan; por defecto `None` = comportamiento
      anterior). `mission.context` guarda `knowledge` y `claims` (JSONB existente).
    - **28 tests** en `tests/test_cognitive_runtime.py`, incluidos los tres obligatorios:
      fallo→diagnóstico→REPLAN→acción alternativa→VERIFY→FINISH; objetivo ambiguo →
      ASK_USER antes de ejecutar nada; claim del modelo sin evidencia → degradado a
      INFERENCE/UNCERTAINTY. Suite completa **290 verde** (los tests de integración con
      PostgreSQL ya no se saltan).
    - Verificado en vivo (Ollama `llama3.2:1b` decidiendo): "borra el archivo
      noexiste.txt" → política pide aprobación → `fs.remove` falla con
      `no existe: /app/workspace/noexiste.txt` → **REPLAN** (diagnóstico `not_found`) →
      ejecuta un paso alternativo con éxito → `verify` → `completed`. La traza queda en
      `mission_events` con topic `cognition.step`.
    - **No implementado (Fase 2, pendiente de aprobación)**: `MemoryProvider` con
      `retrieve` real, World Model consultable, `ModelPlanner`/`PlanValidator`,
      `EnvelopeBuilder`/`CapabilitySelector`, `ResponseComposer` con claims, `Reflector`,
      reanudación de una misión tras ASK_USER.

### Fase 2 — incrementos (reorientación)

Cada incremento es pequeño, con sus propios tests y con un cambio de comportamiento
demonstrable. Ninguno crea contratos ni eventos sin consumidor real.

| # | Incremento | Estado | Comportamiento que cambia |
|---|---|---|---|
| 2.0 | Aislamiento de la base de datos de tests | **hecho** | `tests/db_isolation.py` + `tests/conftest.py`: los tests de integración usan la base `alexis_test` (mismo servidor, base separada). Tres protecciones: (a) guarda que aborta la sesión si la DSN de test apunta a la de desarrollo; (b) **red estructural**: `ALEXIS_DATABASE_URL` se redirige a la DSN de test antes de que se importe `alexis.storage.db`, así que hasta un `Database()` desnudo no puede tocar desarrollo; (c) el fixture `db` migra y trunca el esquema público antes de cada test. Verificado: suite completa → 0 filas nuevas en `alexis`; un test de integración → filas en `alexis_test` y nada en `alexis`. Override con `ALEXIS_TEST_DATABASE_URL` / `ALEXIS_TEST_DB_NAME`. |
| 2.1 | Autoridad del envelope al reconstruir misiones | **hecho** | `mission_from_row` preserva `capabilities`, `perimeters` y `auto_approve`. Antes, tras un reinicio un envelope sin `capabilities` hacía que la regla `capability.outside_envelope` dejara de aplicarse: un paso bloqueado volvía a permitirse. Tests: round-trip, policy tras recarga y reinicio real contra PostgreSQL. |
| H3 | La exigencia de aprobación de Policy es vinculante | **hecho** | `AutonomyGate` solo miraba `step.requires_approval`: si la Policy devolvía `requires_approval=True` (p. ej. `risk.requires_approval` por riesgo high/critical) y el paso no lo declaraba, el gate autorizaba y el paso se ejecutaba. Ahora `policy_required` es vinculante en **todos** los niveles de autonomía y el flag del paso se combina con ella (`step.requires_approval OR policy.requires_approval`) en READ_ONLY, ASSIST y SUPERVISED. En AUTONOMOUS se conserva la semántica previa: manda el envelope (`approval_required`), que es la declaración del usuario, y el flag del paso sigue siendo asesor. Un `DENY` nunca se convierte en solicitud de aprobación. |
| MVP | Cognitive MVP (E2E real, sin código nuevo) | **ejecutado** | Con un repo temporal (`app.py` + `tests/test_app.py`, un test fallando de forma determinista) se ejecutaron misiones reales y se extrajeron las trayectorias de `mission_events` (topic `cognition.step`). **Lo que funciona**: observación → decisión → evidencia (12 claims), World Model aprendiendo, fallo → `replan` → acción distinta, y trayectorias distintas para el mismo objetivo según el contexto (5 decisiones/0 replans/`completed` con el archivo presente vs 8 decisiones/2 replans/`waiting_clarification` sin él). **Lo que NO funciona (rompe el ciclo en dos puntos)**: (1) no existe capability para ejecutar los tests (`execute.test` está en el catálogo como `base` sin adaptador), así que ALEXIS no puede obtener la evidencia del fallo ni verificar el arreglo; (2) `FilesystemVerifier` verifica que el archivo **exista**, no que el objetivo se cumpla — una misión se completó con `verified/passed=true` mientras el test seguía fallando; (3) la escritura de un arreglo real no es posible de extremo a extremo: `fs.write` inyecta contenido placeholder salvo que el plan traiga `args.content` (solo el ModelPlanner puede, y está opt-in); (4) el replan cambia de paso pero no de estrategia (mismo intento de lectura con otro id). |
| H2.1 | Plan Reuse Validation | **hecho** | `AlexisRuntime._ensure_plan` reutilizaba `mission.plan` y `context["plan_steps"]` con `return` temprano: **sin volver a validar**. Como `mission_to_row` no persiste `mission.plan`, tras un restart el plan volvía solo por `context["plan_steps]` y entraba a ejecución sin control. Ahora **toda** entrada de plan —en memoria, deserializada/persistida, del `ModelPlanner` o del `RuleBasedPlanner` (incluido el fallback)— pasa por el mismo `PlanValidator` mediante `_plan_reasons()`; **reutilización ≠ confianza** (no existe flag que la salte). Un plan reutilizado inválido se registra en `plan.invalid` (evento + `context["plan_rejected"]` con `source`: `in_memory` / `context` / `model` / `rule_based*`) y cae al planner por reglas, que también se valida; si tampoco vale, la misión queda `failed` sin ejecutar nada parcial. El paso derivado durante un replan se valida con la misma autoridad (`PlanValidator.validate_step`). Sin `plan_validator` inyectado el comportamiento legacy es idéntico (salvo el camino cognitivo, que siempre se auto-provee uno). **Bug de 2.5 corregido**: el validador rechazaba los planes del propio `RuleBasedPlanner` (`execute` + `fs.read` llegaba como "efectos con capability sin efectos"); `execute`/`test` son verbos neutros cuyo efecto lo declara `CapabilitySpec.side_effects`, así que salieron de `EFFECT_STAGES`. |
| H1 | `AutonomyGate`: el read-only ya no bypasea Policy | **hecho** | `gates.py` consultaba `PolicyEngine` **después** de la rama read-only, así que `analyze/understand/research/verify` obtenían `allowed=True` sin pasar por las reglas de envelope. Ahora la policy se evalúa primero para toda acción: una capability de lectura fuera del envelope se DENIEGA (`capability.outside_envelope`), igual que una escritura. Lo único que la lectura no compra es saltarse una aprobación que la policy o el propio paso exigen. Además `PlanValidator` consulta la `PolicyEngine` real (no la replica) para que un plan no pueda ser aceptado en planificación y bloqueado en ejecución. H1 reveló que 2 tests de Fase 1 declaraban envelopes incompletos y solo pasaban por el bypass. |
| H2 | `PlanStep.args` llega al Executor | **hecho** | `SandboxExecutor` ignoraba `step.args` y re-derivaba el path con `extract_workspace_path(objetivo)`: dos fuentes de verdad para la misma acción. Ahora `_step_args()` da prioridad a `PlanStep.args` (fuente única); `_run_tool()` recibe el dict exacto y **no fusiona nada**; `fs.write`/`fs.remove`/pasos de análisis usan los args del paso; y el tool se resuelve por `step.capability` cuando esta lo identifica sin ambigüedad (`fs.read/stat/write/remove`), para que no se pueda validar una capability y ejecutar otra. Guardia de perímetro en el executor: si los args cambian **después** de validar y salen del workspace, la herramienta no se llama. Las operaciones destructivas sin ruta ya no caen al `default_path`: fallan con honestidad. La copia de `PlanStep` en un replan preserva `args` (era un segundo punto de pérdida). |
| 2.5 | `ModelPlanner` + `PlanValidator` | **hecho** (opt-in) | `alexis/cognition/planner_model.py`. El modelo **propone** un DAG (`ModelTask.PLAN`, etapas de vocabulario cerrado) y un validador **determinista** decide si se acepta; si no, cae al `RuleBasedPlanner` y el motivo queda en `plan_provenance` + evento `plan.invalid`. Valida: capability existente; capability **disponible** (catálogo ≠ disponibilidad, coherente con 2.3); dependencias resolubles, sin ciclos y sin autodependencia; coherencia con el envelope (el plan **no puede** ampliar `capabilities`/`perimeters`/`auto_approve`); argumentos que no salgan del perímetro; riesgo no subestimado; **el plan no puede quitarse una aprobación** (capability crítica sin `requires_approval` → REJECT); y relevance (si el objetivo nombra una ruta, el plan no puede proponer otras). `PolicyEngine`/`AutonomyGate` siguen siendo la autoridad: el validador no duplica la policy y cada paso se vuelve a autorizar. `PlanStep` gana `args` y `expected` (retrocompatible) y `plan_to_dict/from_dict` ya los persisten. **Activación**: `ALEXIS_MODEL_PLANNER=1`; por defecto OFF porque con `llama3.2:1b` en esta máquina un plan tarda 46-60s y suele ser inválido. |
| 2.4 | World Model operativo y consultable | **hecho** | `alexis/world/model.py` pasa de 23 líneas sin consumidores a un modelo que (a) **se alimenta de la realidad**: `observe_execution()` extrae FILE entities de lo que las tools devolvieron (`path`, `exists`, `size`) con su `source` y `confidence=0.7` (es EVIDENCIA, no un hecho verificado); (b) **se consulta**: `query()`, `for_objective()`, `known_path()`, `missing_paths()`, `neighbors()`/`dependencies()`/`relate()`; (c) **gobierna decisiones**: si el mundo ya observó que la ruta del objetivo no existe, `world_blocked_ids()` marca como inviables las capabilities que fallan previsiblemente sobre algo ausente (`fs.read`, `fs.stat`, `research.filesystem`, `fs.remove` — `fs.write` NO, porque crear lo que falta es su trabajo) y las que dependen de ellas. Si no queda nada viable → `ASK_USER` con la evidencia; si queda algo → ejecuta eso y deja el hueco en `hypotheses`. Verificado en vivo con el planner real: "borra el archivo fantasma-77.txt" → 2 ejecuciones y pregunta basada en observación, frente a 6 ejecuciones (2 reintentos superfluos) sin World Model. |
| 2.3 | Self Model como entrada de decisión | **hecho** | `CognitiveRuntime` recibe el Self Model y consulta `SelfBrief` **antes de decidir**. Si el objetivo exige capabilities que ALEXIS no tiene, pregunta (`ASK_USER`) **sin ejecutar ninguna tool**; si solo falta alguna, hace lo que puede y deja el hueco en `knowledge.unknown`. Un paso cuyo `depends_on` no puede correr tampoco se considera ejecutable. Lo que ALEXIS tiene pero el envelope no autoriza lo sigue decidiendo `PolicyEngine` (no se mezcla con la metacognición). En la otra dirección, `SelfModelSync` consume `cognition.step` y refleja replans, fallos, preguntas y procedencia (`degraded`) como auto-observaciones y presencia. **Bug corregido**: `SelfModel.available` por defecto era el catálogo completo (30 caps, incluidas las `missing`): afirmaba tener `git.read`; ahora el demo declara las 14 habilitadas. |
| 2.2 | `MemoryProvider` mínimo con recuperación real | **hecho** | La memoria participa **antes de decidir**: `PostgresMemoryProvider`/`InProcessMemoryProvider` recuperan observaciones por relevancia real (términos compartidos) desde la tabla `observations` que el runtime ya escribía; `InMemoryMemory.recall` (que ignoraba la query) deja de ser el camino. El contexto entra sanitizado (`as_prompt_lines` → `sanitize_untrusted`) en el prompt de decisión y queda en `knowledge.memory`/`experience`. Verificado en vivo: 5 observaciones de misiones anteriores recuperadas y la pregunta final al usuario las incluye. |

Pendientes: Self Model como entrada de decisión · World Model consultable ·
`ModelPlanner`+`PlanValidator` · selección dinámica de capabilities · verificación por
`success_criteria` · `ResponseComposer` · aprendizaje persistente.

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
