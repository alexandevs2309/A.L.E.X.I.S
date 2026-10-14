# VISION → Reality alignment

`docs/VISION.md` is the north-star. This file answers the only question that matters when
reading it: **of what it describes, what does ALEXIS actually do?**

It exists because `VISION.md` §69 rule 20 requires capabilities that cannot be implemented to
be *registered* as `PLANNED`/`MISSING`/`UNAVAILABLE`, and rule 4 forbids marking anything
complete without real implementation and tests. A 2000-line target document with no
reconciliation would do the opposite of both rules: it would let aspiration read as fact.

**Verification date**: 2026-10-10 · commit `e8b6c82` + correcciones de cierre · **1628 deterministic tests passing** (`-W error`, con PostgreSQL). Sin `ALEXIS_DATABASE_URL` sólo pasan 1505: **123 se omiten** y la persistencia no queda demostrada.

> **Nota**: las tablas §16/§20 de este documento son el inventario histórico de 2026-10-03. La re-auditoría vigente de los 22 requisitos P0 está en `docs/P0-COGNITIVE-CORE-GAP-ANALYSIS.md` §8. Varias capacidades que aquí figuran como `MISSING` ya existen (`browser.research` está `available` con tool propia; `health_monitor.py` y `critic.py` están implementados).

## How to read the statuses

| Status | Meaning |
|---|---|
| `IMPLEMENTED` | Real code, wired in the production runtime (`apps/demo`), covered by tests |
| `PARTIAL` | Real code, but a narrower slice than the vision describes |
| `CONTRACT` | Interfaces and data structures exist; no working behaviour behind them |
| `MISSING` | Nothing, but the architecture has a place for it and it is registered |

Nothing is marked complete on the strength of a doc, a class name, or a passing isolated test.

---

## 1. Foundation — what genuinely works today

| Vision § | Capability | Status | Evidence |
|---|---|---|---|
| §2 | Canonical loop | `IMPLEMENTED` *(corregido 2026-10-11; decía `PARTIAL`)* | `alexis/cognition/loop.py` — decide→policy→execute→observe→evaluate→replan→verify, **y el epílogo reflection→experience→learning**, que la fila daba por ausente. Está cableado en el ciclo (`loop.py:2075-2156`): `build_reflection()` produce la reflexión, `_build_experience()` la convierte en experiencia y `LearningBoundary().evaluate()` decide si la lección merece la frontera de aprendizaje. La lección se propone en función de `goal_verified` y de la calidad de la evidencia, no de que el paso terminara. La afirmación anterior era correcta cuando se escribió y dejó de serlo al aterrizar el epílogo; nadie la actualizó |
| §4.1 | Intelligence separate from capabilities | `IMPLEMENTED` | `alexis/cognition/planner_model.py` proposes; `PlanValidator` disposes; `PolicyEngine`/`AutonomyGate` authorize |
| §4.2 | Data is not authority | `IMPLEMENTED` | Untrusted content is marked as data in every prompt path; `ClaimGuard` (`alexis/cognition/evidence.py`) never promotes a model claim to FACT |
| §4.3 | Autonomy inside explicit authority | `IMPLEMENTED` | `MissionEnvelope`, `alexis/security/policy.py`, `alexis/autonomy/gates.py` |
| §4.4 | No fake capabilities | `IMPLEMENTED` | Capability catalog carries a real `status` per spec; `available()` ≠ `enabled()` |
| §4.5 | Security outside model authority | `IMPLEMENTED` | No path lets a plan widen its envelope, lower its risk, or drop `requires_approval` — tested in CORE-08/08B.1 |
| §4.6 | Completion requires independent verification | `IMPLEMENTED` | `Mission.__setattr__` refuses `COMPLETED` without a confirming `GoalVerification` (`354a388`) |
| §5 | Self Model | `PARTIAL` | `alexis/self/model.py`; learned state persists (`93c6930`). Missing: goals, commitments, skills, health, model_state |
| §7 | World Model | `PARTIAL` | `alexis/world/model.py` — entities, scopes, staleness, conflicts, provenance, persistence. Only FILE/TEST domains; no projects, devices or time |
| §8 | Memory | `COMPLETE` *(2026-10-10)* | `tests/test_memory_module_closure.py` (17 tests) + `test_memory_provider.py` (10); fixes MEM-1..MEM-4: presupuesto de tokens, aislamiento por misión, sanitización del estado persistido y orden determinista. Los **7 tipos conceptuales de `MINDMAP.md` §4 están implementados de forma distribuida**: Working→`missions.context`, Episodic→`observations`+`memory.provider`, Semantic→`world_entities`, Procedural→`skills`, Project→`missions.goal`+`world_edges` (todos COMPLETE). Vector: pgvector en uso (columna `observations.embedding vector(384)` + índice HNSW, `PostgresMemoryProvider` ordena por coseno con fallback a términos, `tests/test_memory_vector_search.py`); Knowledge Graph PARTIAL; **Preferences COMPLETE** (`alexis/user/`, tabla `user_preferences`, migración 065, `tests/test_user_model.py`): categorías `project`/`style`/`approval`/`context`, zonas `user` del SelfModel, inyección en `mission.context` y API `/user/*`. Knowledge Graph sigue PARTIAL: **feature futura, no deuda del CORE** |
| §11 | Success criteria | `IMPLEMENTED` | `GoalVerifier` checks tool-observed predicates against real observations; a model assertion is never enough |
| §13 | Replanning and recovery | `IMPLEMENTED` | Deterministic filter (`loop.py`), plus dynamic replanning via `ModelTask.PLAN` with a validated overlay (`31ab3b8`) |
| §17 | Capability system | `PARTIAL` | 30 specs: 17 `available`, 1 `base`, 12 `missing`. **8** con adaptador registrado en `ToolRegistry` (`fs.read/stat/write/remove`, `git.read`, `git.commit`, `browser.research`, `execute.test`); las otras 10 declaradas operativas son capacidades internas del runtime (`cognition.*`, `autonomy.*`, `execution.sandbox`, `verification.filesystem`, `research.filesystem`, `desktop.tools`, `perception.clap`, `tts.speak`), que no son tools. `CapabilitySpec` declara `id`, `sphere`, `network`, `side_effects`, `trust_domain`, `sandbox_profile`, `default_risk`, `audit`, `plans_action`, `requires_input`, `status`, `resources`, `description` — **no** declara `outputs`, `permissions`, `environment requirements` ni `verification method`, que §17 sí enumera; la afirmación anterior de que ese catálogo sí los declaraba era falsa |
| §22 | Model Router | `IMPLEMENTED` | `alexis/models/router.py` — REAL/DEGRADED/UNAVAILABLE, chain, per-mission budgets (`c7a259e`), full provenance (`8bfc102`) |
| §32 | Persistence and recovery | `PARTIAL` | PostgreSQL for missions, world, knowledge, evidence, audit. Restart resumes a mission; destructive replay is not implemented |
| §35 | Security architecture | `PARTIAL` | Identity→Capability→Policy→Envelope→Gate→Sandbox→Execute→Observe→Verify→Audit all present. No network allow-list layer |
| §36 | Secret boundary | `PARTIAL` | `secrets/` is gitignored; `scripts/check_secrets.py` blocks staged credentials. No redaction layer inside prompts |
| §54 | Observability and audit | `IMPLEMENTED` | Every model call, policy decision, verification and replan is recorded with correlation |
| §53 | Cloud failure strategy | `IMPLEMENTED` | Provider chain → local → deterministic fallback, preserving provenance |

## 2. Registered but not implemented

These appear in `VISION.md` and have an architectural place, but **no working behaviour**.
Listed so they are not mistaken for absent by accident, nor for present by optimism.

| Vision § | Capability | Status | Where it would live |
|---|---|---|---|
| §6 | User Model | `COMPLETE` *(2026-10-11)* | `alexis/user/` + `scripts/migrations/065_user_preferences.sql` + `tests/test_user_model.py` (26 tests): preferencias por categoría, proyectos activos, decisiones recientes, zona `user` en `SelfModel` y API `/user/*` |
| §9, §10 | Goal management & decomposition | `IMPLEMENTED` | `alexis/autonomy/goal_state.py` + `GoalTracker`;Scheduler en `alexis/autonomy/` |
| §14 | Causal reasoning | `MISSING` | `diagnose_failure` classifies failure kinds; it does not build causal chains |
| §15 | Experiment engine | `CONTRACT` | `alexis/experiments/engine.py` — interface only |
| §16 | Autonomous research | `IMPLEMENTED` | `browser.research`: tool propia (`alexis/tools/browser.py`), allowlist + SSRF por salto + descarte de prompt injection, 61 tests |
| §18, §19 | Capability discovery & tool creation | `MISSING` | No inspection→evaluation→approval→registration pipeline |
| §20, §21 | Skill system & learning lifecycle | `IMPLEMENTED` *(corregido 2026-10-13; decía `PARTIAL`)* | Ciclo completo y demostrado con el tipo que el runtime usa de verdad: `experience.py` → `lesson.py` → `skill.py`, orquestado desde `core/runtime.py::_advance_learning`. Persiste en `lessons`, `skills` y `observations` — **no** en `self_learnings`, que escribe `alexis/self/persistence.py` y es otro subsistema. La afirmación anterior atribtaba a este módulo una tabla que no toca |
| §25 | Resource budgets | `PARTIAL` | Cost and time budgets exist per mission; no token/call/disk budgets |
| §26 | Commitment manager | `MISSING` | — |
| §27 | Scheduler | `PARTIAL` *(reevaluado 2026-10-13; decía `CONTRACT`)* | **La descripción anterior era falsa y sugería menos de lo que hay.** `autonomy/scheduler.py` (51 líneas) NO es «una interfaz»: hace polling de tareas reclamables, aplica `deadline` y recupera leases expiradas (`recover_stale`), y está cableado en producción (`OfficialRuntime.scheduler`, invocado desde `_recover_missions`). `autonomy/schedule_rules.py` (332 líneas) implementa además `one_time`/`interval`/`cron` con persistencia PostgreSQL y validación *fail-closed* del envelope. **Lo que falta no es la interfaz: es el consumidor.** `schedule_rules` tiene 0 filas, no existe `POST /schedule`, y nada ejecuta las reglas ni crea las misiones. Corregidos R-01 (los rangos de cron lanzaban `TypeError`) y R-02 (con un campo `*` la restricción del otro se ignoraba: `0 0 1 * *` se disparaba a diario) |
| §28 | Event & trigger engine | `PARTIAL` *(sin cambios: la clasificación era correcta)* | `EventBus` es real: `publish` + `subscribe`/`subscribe_async`, con suscriptores en producción (`models/audit_sink.py`, `self/sync.py`, `apps/api/main.py`, `apps/demo/server.py`). **Verificado que todos son sumideros: ninguno origina trabajo configurable.** La única creación de misión desde un evento es la activación por voz (`server.py:246`) con objetivo y envelope **hardcodeados**, sin pipeline FILTER→CONTEXT→POLICY→DECISION. El requisito normativo de §28 —«Not every event should automatically cause action»— se cumple por omisión: no hay eventos externos que puedan causar acción. Fuentes externas (GitHub, filesystem, email, calendar, MQTT, timers): ninguna |
| §29, §30 | Self-health & model quality monitoring | `MISSING` | No degradation detection, no provider history |
| §31 | Independent criticism (ModelCritic) | `IMPLEMENTED` | `alexis/cognition/critic.py` |
| §33 | Uncertainty engine | `PARTIAL` | Confidence and uncertainty are tracked in `KnowledgeState`; not a first-class subsystem |
| §34 | Operational explainability | `PARTIAL` | Structured decision metadata exists; not surfaced as a first-class audit view |
| §45, §46 | Sub-agents & multi-agent | `MISSING` | `AgentRegistry` is a registry; no delegation with scoped authority |
| §48 | Long-term autonomy | `MISSING` | Single missions only |
| §49, §50 | Human collaboration modes & autonomy levels | `PARTIAL` | Autonomy levels exist; PAUSE/STOP/CANCEL/DENY are partial |
| §57 | Self-diagnostics | `MISSING` | — |
| §58 | Self-improvement | `MISSING` | Must stay bounded (rule 19). Nothing exists yet |
| §59 | Knowledge graph | `CONTRACT` | WorldModel relations are the seed; no graph semantics |
| §60 | Temporal reasoning | `PARTIAL` | Timestamps and staleness exist; no before/after/during reasoning |
| §61 | Predictive monitoring | `MISSING` | — |
| §62 | Opportunity detection | `MISSING` | — |
| §63 | Personal OS layer | `PARTIAL` | Orchestrates filesystem + tests + desktop. Not yet the user's whole environment |

## 3. Capabilities with no real adapter

Registered in the catalog with `status=missing`, so the system reports them honestly instead
of pretending: `git.push`, `terminal.run`, `api.http`, `mcp.run`, `codex.run`, `opencode.run`,
`obsidian.run`, `omniroute.run`, `hermes.run`, `vision.screen`, `speech.stt`, `iot.mqtt`.

**Corrected 2026-10-13.** This list previously also contained `git.read`, `git.commit` and
`browser.research` as `missing`. That stopped being true when those three got real adapters
and tests; the list was never reconciled, so it understated what works. `git.push` IS still
`missing` on purpose — the adapter exists but the operation cannot run inside the sandbox.

Real adapters exist for: `fs.read`, `fs.stat`, `fs.write`, `fs.remove`, `execute.test`,
`desktop.tools`, `execution.sandbox`, `research.filesystem`, `verification.filesystem`,
`tts.speak`, `cognition.understand`, `cognition.analyze`, `autonomy.gates`, `autonomy.queue`,
`perception.clap`.

> `verification.filesystem` is registered and available but **has no tool implementation**
> (`build_filesystem_tools` returns only the four `fs.*` tools). It must not be treated as
> independently observable evidence of existence.

## 4. Presence, voice, vision, physical

| Vision § | Capability | Status |
|---|---|---|
| §39 | Vision | `MISSING` — `vision.screen` registered, no adapter |
| §40 | Audio & voice | `PARTIAL` *(descrito 2026-10-13; la fila anterior sólo decía «tts.speak real; STT/VAD/barge-in absent»)* | **TTS es real y se ha ejecutado**: 4 proveedores (`speech/edge.py`, `elevenlabs.py`, `local.py`, `noop`), selección por entorno y *fallback* con **error honesto** —«para que no haya silencios fingidos» (`tts.py:108`). Ruta contractual: `action="respond"` → `on_mission_end` (`server.py:272`) → `_announce_voice` (`:111`) → `synthesize_with_fallback` → `_publish_voice` (`:91`) → directorio vigilado por el `voice_bridge` del host (`aplay`/`gst-play`). **No es una `Tool` y no lo necesita**: `tts.speak` no está en `ToolRegistry` y `available` en el catálogo es correcto. Hay **52 MP3 reales** en `.cache/tts/` con el patrón `{mission_id}-{ms}.{ext}`, lo que acredita ejecución del runtime — **pero no acredita que el sonido saliera por el altavoz**. También implementados y cableados: *wake word* (`perception/wakeword.py`), detección de palmada (`clap_detector.py`, `clap_listener.py`) y selección de micrófono (`microphone.py`), con entrada por `server.py:292-305`. **Ausentes:** STT (sólo `Protocol`, sin implementación), VAD, barge-in, streaming TTS, speaker recognition, turn management, audio event perception |
| §41 | Presence engine | `IMPLEMENTED` *(reevaluado 2026-10-13; decía `PARTIAL` sin dar razón)* | Motor de presencia, **no** el avatar. `derive_presence()` (`self/presence.py:81`) es una función pura que traduce el estado **real** de la misión a una etiqueta; `SelfModel` la invoca en cada derivación (`self/model.py:204,378`) y expone `current_state` en el snapshot (`:394`). Su consumidor real es `SelfBrief.from_snapshot` (`cognition/contracts.py:161`). **El avatar NO lee ese campo**: `_state_payload()` (`apps/demo/app.py:1036`) no lo expone y `mapCoreState` (`apps/face/src/state/CoreStateAdapter.ts:36`) vuelve a derivar su estado desde `mission.state` y el último paso. Son dos derivaciones sobre la misma misión, no una; corregido el 2026-10-13 tras verificarlo (la fila anterior afirmaba lo contrario). **Los 16 estados** de `PRESENCE_ORDER` coinciden uno a uno con los de §41. **Las 6 fuentes del `should` de §41 están**: Self Model, Cognitive Runtime (`cognition.step` → `_apply_cognition`), mission state (`_STATE_PRESENCE`), voice state (`presence.speaking/idle`), perception state (`presence.listening`) y tool state (`_ACTION_PRESENCE`, desde la acción del último paso). Cumplidos los **dos `must`**: consume estado interno real —los eventos que lo alimentan los publica el runtime (`mission.step_started/completed`, `policy.evaluated`, `mission.replanning`, `cognition.step`) y la voz/clap—, y **no persiste** el estado, de modo que no puede quedarse obsoleto mostrando actividad: se deriva siempre (`derive_presence(None)` → `idle`). Cubierto por `tests/test_self_model.py` (mapeo exhaustivo de estados y `current_state` real), `test_cognitive_self_model.py` y `test_cognition_contracts.py`. **LIMITACIÓN: el renderizado real del avatar no se ha verificado de extremo a extremo y queda fuera de este cierre** — lo verificado es el motor que produce el estado, no la representación visual (que pertenece a §42) |
| §42 | Advanced avatar | `PARTIAL` *(reevaluado 2026-10-13; decía `CONTRACT`, que significa «sin comportamiento detrás»)* | `apps/face/` es una aplicación Three.js real: 2387 líneas de TypeScript en 22 módulos, con bundle compilado (`dist/`, 574 KB, contemporary con `src/`). Comportamientos con módulo propio: parpadeo (`BlinkController`, 6 tipos con variación natural), mirada (`GazeController`), movimiento de cabeza, morph targets, expresiones, micro-movimientos, visemas y lip sync (`VisemeController`, `LipSyncEngine`), materiales neuronales y partículas (`NeuralMaterial`, `ParticleSystem`), y reconocimiento de voz (`SpeechRecognizer`). **Sincronización con voz real**: si `voice.speaking`, el bucle fuerza `state:"speaking"` (`main.ts:191`). Degrada honestamente: `fetch("/state")` con `.catch`, y sólo muestra la misión que esa sesión abrió. **Deriva su propio estado desde `mission.state` y el último paso** (`CoreStateAdapter.mapCoreState`), no desde `current_state` de §41. **Las 15 capacidades de «Future avatar capabilities» (`VISION.md:1265`) son futuras, no obligaciones**: su presencia como módulo o en el bundle **no** acredita que funcionen en producción. **LIMITACIONES: no existe ninguna suite de pruebas del avatar (cero `*.test.ts`) ni evidencia de ejecución visual de extremo a extremo** — por eso `PARTIAL` y no `IMPLEMENTED`, que exigiría «covered by tests» |
| §43 | Desktop perception | `PARTIAL` *(reevaluado 2026-10-13)* | **De las 8 áreas que enumera `VISION.md:1286-1302` sólo 1 está implementada: application launching.** `desktop.tools` registra 6 herramientas (`chrome.open_url`, `spotify.play`, `claude.open`, `binance.open`, `cursor.open`, `tts.speak`) que **delegan la acción en el host** vía `webbrowser.open`, `subprocess.Popen` y `shutil.which` (`alexis/tools/desktop.py`), consumidas por el `desktop_bridge` y despachadas por el executor con verificación de misión. **Esto es automatización de escritorio, NO percepción**: `desktop_bridge` consulta el estado de **ALEXIS** (`/state`) antes de actuar, y en ningún caso observa el escritorio. **AUSENTES — sin implementación:** captura de pantalla (sólo el `Protocol` `ScreenPerception`, 0 implementaciones), detección de aplicaciones, estado de ventanas, extracción de texto (OCR), comprensión de interfaz, control de ratón/teclado e inspección de procesos (0 coincidencias en el árbol). Las 8 áreas son **aspiracionales** (`should eventually`), no requisitos; por eso su ausencia no es un incumplimiento. **PRUEBAS ≠ EJECUCIÓN REAL:** `tests/test_desktop_tools.py` (37 tests, todos pasan) verifica registro, riesgos, dispatch del executor y verificación de misión, **siempre con proveedores inyectados** (`_FakeProvider`, `monkeypatch`); **no demuestran ejecución real en el host**. **AUTORIZACIÓN — requisito normativo satisfecho:** `VISION.md:1302` exige *«Desktop control remains subject to policy and authorization»*; el catálogo declara `network=True`, `side_effects=True`, `trust_domain="host"`, `sandbox_profile="host-delegated"`, `default_risk="medium"`, y cada tool declara su riesgo; la autorización la aplican el gate y la policy en runtime, **no** el estado del catálogo |
| §44 | Physical world & IoT | `MISSING` — `iot.mqtt` registered; no adapter. Correct: physical control needs stricter risk classes |

## 5. Where the vision and the code disagree on purpose

Four places where the vision is stricter than the implementation, recorded rather than
quietly resolved in either direction:

1. **§12 Dynamic Planning** — the vision expects plans carrying `preconditions`,
   `expected effects`, `alternatives`, `rollback`. `PlanStep` has `objective` and
   `success_criteria` fields but the planner does not populate them. Not a filter, not
   verified: incomplete.
2. **§11** — the vision implies any meaningful goal can be verified. Today only tool-observed
   predicates are. A goal in free language with no checkable predicate stays
   `insufficient_evidence`, which is the honest outcome, not a satisfied one.
3. **§2 loop** — the canonical loop ends `REFLECT → LEARN → UPDATE`. The loop itself stops
   at `VERIFY → COMMIT`. **Corrected 2026-10-13:** this paragraph said reflection «is not an
   automatic stage that feeds the next decision». That was half true and hid a real outage.
   The runtime epilogue (`core/runtime.py::_finalize`) *does* run
   `_advance_learning` automatically on every verified mission, and `hydrate_learning()`
   loads previously validated skills back into planning. What genuinely does not exist is
   the reflexion feeding the *cognitive loop's* own decision — a different, smaller gap,
   in COGNITION, not here. The stage was automatic; it was **inert**, because
   `record_experience` read a `Verification.verified` attribute that does not exist and so
   recorded `goal_verified=False` for every mission, forever. See R-01 below.
4. **§27 Scheduler y §28 Event & trigger engine — `PARTIAL`, y la fase contractual puede
   considerarse resuelta (2026-10-13).** Una auditoría contractual de ambas secciones
   concluiu que **ninguna impone un requisito obligatorio incumplido**, y conviene no
   confundir *función pendiente* con *deuda contractual*:

   - **§27 no obliga hoy a ejecutar reglas programadas.** Su único verbo es
     «Support *eventually*» (`VISION.md:831`); no hay un solo `must` en sus 33 líneas. Su
     única afirmación normativa es de **autoridad** —«Scheduling remains policy-controlled»
     (`:858`)— y **se cumple**: `validate_envelope_template()` (`schedule_rules.py:104`) es
     *fail-closed* y toda regla lleva su `envelope_template`.
   - **§28 no obliga hoy a un motor de triggers.** «*Potential* event sources» (`:864`) y un
     diagrama de pipeline sin verbo normativo. Su única afirmación normativa —«Not every
     event should automatically cause action» (`:903`)— **se cumple de forma vacua**: no hay
     ninguna fuente externa capaz de causar acción.
   - **§67 no bloquea el avance.** «The presence of a capability… does **not** mean it must
     be implemented immediately» (`:2008`); establece un *orden*, no un cierre. Y este
     documento «no autoriza trabajo» (`:154`).

   **Lo que sí queda registrado como capacidad futura, no como requisito vigente:**
   ejecutar las reglas persistidas (hoy `schedule_rules` tiene 0 filas y **ningún consumidor**
   — la única mención fuera del módulo es un comentario SQL en `storage/schema.py:114`) y el
   motor configurable de triggers. Ambas se registrarían como `PLANNED` si se acometen a
   construirlas (regla 20).

   **Una incoherencia que NO es incumplimiento contractual:** `ScheduleRule` modela estado de
   ejecución (`last_run_at`, `consecutive_failures`, `max_consecutive_failures`) que **nadie
   escribe ni nadie lee** fuera del propio módulo. §27 no menciona persistencia en ningún
   punto, así que *store-sin-ejecución* no viola ninguna cláusula; es deuda de diseño
   latente, no deuda contractual.

   **Restricción que debe preservarse al construirlas:** ambas habilidades ejecutan trabajo
   sin intervención directa del usuario, así que la autoridad no puede relajarse —
     `envelope_template` validado *fail-closed*, sin `auto_approve_destructive` implícito, y
     el gate y la policy siguen siendo los únicos que autorizan.

   **Pendiente de decisión contractual antes de que exista un consumidor:** la
   convención del día de la semana en cron. `cron_next()` compara con `datetime.weekday()` de
   Python (`0 = lunes`); el cron estándar usa `0 = domingo`. Hoy es inocuo porque nada lo
   consume, pero cualquier regla que dependa de ella quedaría mal interpretada.

## 6. What this means for the next increment

The vision's own §67 priority order puts **core correctness → safety → persistence** first,
then goal management, then recovery, then learning. Read against the real state:

- The P0 cognitive core is at **22 COMPLETE · 0 PARTIAL of 22** (100% by its own scoring method) with the method stated in
  `P0-COGNITIVE-CORE-GAP-ANALYSIS.md`. There are no remaining P0 gaps: **Req 2** closed with a
  PostgreSQL end-to-end test (`bb74a16`) and **Req 3** closed by design decision —the
  SelfModel informs, the Policy authorizes, deliberately, so a stale SelfModel cannot
  block legitimate work.
- **Reflection (§17) is `COMPLETE`, not `MISSING`.** This paragraph used to say it was the
  smallest gap the vision needed and the code did not have. It is implemented
  (`alexis/learning/reflection.py`, `experience.py`), wired into the loop's epilogue
  (`cognition/loop.py:2078`, called from `core/runtime.py:544`) and covered by
  `tests/test_p0_5_6_epilogue.py`. The gap analysis marked it COMPLETE on 2026-09-26; this
  paragraph was never updated to match the document it refers to.
- Goal management (§9, §10) is the largest structural absence: ALEXIS manages one mission at a
  time and has no notion of an objective outliving it.

**Nothing in this file authorises work.** It records distance. What to do next is a decision
for the user, not something an agent should infer from a roadmap.

---

*Aligned with `VISION.md`. If the two ever disagree, `VISION.md` describes the target and
this file describes the system; the truth about the system is in the code and its tests.*