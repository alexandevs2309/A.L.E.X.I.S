# ALEXIS — F2: Cognitive Core (especificación)

> Estado: **especificación para revisión**. No implica código implementado.
> Precedente: `docs/AUTONOMY-V0.5-CAPABILITIES.md` (F1 — Capabilities) y
> `docs/SELF-MODEL.md` (F0 — Self Model).
> Objetivo: que ALEXIS deje de ser un runtime que ejecuta capacidades y pase a ser una
> inteligencia que **entiende, decide, usa lo que puede, verifica lo que dice y explica
> lo que hizo**.

---

## 0. Nota de nomenclatura (importante)

`docs/AUTONOMY-V0.5-CAPABILITIES.md §8` ya usaba los nombres F1–F5 para la línea de
capacidades. **Renombrado aprobado** (no rehace F1; solo cambia la etiqueta):

| Línea de capacidades (C) | Significado | Estado |
|---|---|---|
| **C1 — Capability Foundation** | Modelado de capacidades (lo que fue F1) | **completado** |
| **C2 — Web Research** | browser-sandbox, evidence web, verificación de investigación | pendiente |
| **C3 — External Integrations** | Obsidian, OmniRoute, Hermes, Codex, OpenCode, MCP, APIs | pendiente |
| **C4 — Advanced Capabilities** | verificación por dominio, lectura de proyecto, `vision.screen` | pendiente |
| **C5 — Environment / IoT / etc.** | MQTT/Home Assistant, sensores, actuadores | pendiente |

**F2 queda reservado exclusivamente para Cognitive Core** (este documento).
`F0` = Self Model, `F1` = Capabilities foundation, `F2` = Cognitive Core.

---

## 1. Objetivo y no-objetivo

### 1.1 Transformación de la cadena

Hoy (F1):

```
Usuario → [POST /missions] → Mission → Planner(fijo) → Capability → Gate/Policy → Execution → Verification
```

F2:

```
Usuario (habla)
  → comprensión de intención          (modelo)
  → contexto relevante                (MemoryProvider)
  → Self Model (brief)                (SelfModel)
  → razonamiento                      (modelo)
  → decisión                          (modelo + catálogo)
  → selección de capabilities         (CapabilitySelector + catálogo)
  → planificación dinámica            (ModelPlanner + validator)
  → envelope (permisos)               (EnvelopeBuilder)
  → policy / approval                 (F1: sin cambios)
  → ejecución                         (F1 + dispatch por capability)
  → observación                       (ExecutionResult + Observations)
  → evidencia y claims                (EvidenceStore + ClaimGuard)
  → evaluación                        (modelo + MetaCognition)
  → replanning                        (ReplanController, acotado)
  → verificación                      (verificadores deterministas + crítico)
  → reflexión                         (Reflector)
  → respuesta natural                 (ResponseComposer)
  → memoria + Self Model actualizados (MemoryProvider + SelfModel)
```

**"Misión" sigue siendo interna**: es el mecanismo de trabajo del Core, no la puerta de
entrada del usuario. El usuario conversa (`ConversationSession`); el Core decide si
hay una misión o no.

### 1.2 No-objetivo de F2

- No implementar C2–C5 (web research, integraciones externas, capacidades avanzadas,
  IoT). F2 deja los *adaptadores* como contratos y las capabilities siguen declaradas
  `missing` hasta que exista un adaptador real.
- No tocar invariantes de seguridad de F1 (ver §16).
- No un sistema multi-agente autónomo ni memoria semántica completa: se dejan las
  interfaces.

### 1.3 Precisiones vinculantes (aprobadas por el usuario)

Estas tres reglas son **de cumplimiento obligatorio** en F2 y en todo lo que herede
este diseño. No son preferencias: son invariantes.

**P1 — Tri-estado de la procedencia de una respuesta cognitiva**

```
REAL       = razonamiento / model output real de un provider real
DEGRADED   = comportamiento determinista de contingencia (sin modelo real detrás)
UNAVAILABLE = no existe un proveedor utilizable
```

- `DegradedModel` se mantiene como fallback, pero **nunca se presenta como
  razonamiento real**.
- `DEGRADED` se propaga hasta la respuesta final (el usuario puede saber que ALEXIS
  actuó sin modelo) y queda registrado en `model.routed` + audit.
- `UNAVAILABLE` es un tercer estado, no un synonym de `DEGRADED`: significa que no hay
  provider; el Core lo dice honestamente en vez de fabricar contenido.
- El campo `ModelResponse.outcome` (`real | degraded | unavailable`) es la única fuente
  de este estado; ningún componente lo infiere.

**P2 — Separación estricta de responsabilidades**

```
Cognitive Core  decide QUÉ necesita hacer (intención, plan, capabilities, claims)
Model Router    decide QUÉ proveedor/modelo puede satisfacer esa necesidad
```

- OmniRoute será posteriormente un **adapter/provider externo**, nunca el cerebro de
  ALEXIS.
- El Core **no depende** de OmniRoute ni de ningún provider concreto: solo del contrato
  `ModelProvider` y del `ModelRouter`.

**P3 — Invariantes que se mantienen desde F1/F0**

1. `greeting` no crea misión.
2. `capability-query` no crea misión.
3. Sólo `IntentKind.TASK` crea misión.
4. `PolicyEngine` / `AutonomyGate` siguen siendo la autoridad.
5. El modelo nunca puede otorgarse permisos.
6. `ModelCritic` nunca puede aprobar por sí solo.
7. La verificación independiente sigue siendo obligatoria.
8. `replan` limitado; nunca loops infinitos.
9. Nunca reportar éxito sin evidencia/verificación.

---

## 2. Arquitectura

### 2.1 Capas

```
┌──────────────────────────────────────────────────────────────────────┐
│ CONVERSACIÓN (puerta principal)                                       │
│ ConversationSession: recibe turnos (texto, voz futura, UI)             │
│ → NO crea misiones para saludos/preguntas de capacidad               │
└───────────────┬──────────────────────────────────────────────────────┘
                │ Turn (utterance, history, self_brief)
┌───────────────▼──────────────────────────────────────────────────────┐
│ COGNITIVE CORE (nuevo)                                                │
│                                                                      │
│  IntentInterpreter ──► SelfModel.brief() ──► MemoryProvider.retrieve  │
│          │                   │                     │                  │
│          ▼                   ▼                     ▼                  │
│  Reasoner (modelo) ◄── contexto + memoria + self                     │
│          │                                                              │
│          ▼                                                              │
│  CapabilitySelector ◄── CapabilityRegistry (F1)                         │
│          │  (propone ⊂ disponible ⊂ envelope)                          │
│          ▼                                                              │
│  Planner (ModelPlanner + PlanValidator | RuleBasedPlanner)            │
│          │                                                              │
│          ▼                                                              │
│  EnvelopeBuilder ──► Mission (Mecanismo interno)                       │
└───────────────┬──────────────────────────────────────────────────────┘
                │ Mission con plan dinámico
┌───────────────▼──────────────────────────────────────────────────────┐
│ F1 (sin cambios de diseño)                                            │
│  AutonomyGate + PolicyEngine(rules) → Approval → Sandbox → Execution   │
│  → Observation → Verification → Learning                              │
│  SelfModelSync (eventos) · Presence (derivada)                        │
└───────────────┬──────────────────────────────────────────────────────┘
                │ eventos semánticos
┌───────────────▼──────────────────────────────────────────────────────┐
│ RESPUESTA                                                            │
│ EvidenceStore + ClaimGuard → Verification agregada → Reflector        │
│ → ResponseComposer → UserReply (texto natural + claims estructurados) │
│ → SelfModel.update + MemoryProvider.store                             │
└──────────────────────────────────────────────────────────────────────┘
```

### 2.2 Módulos nuevos (propuesta)

```
alexis/cognition/
  core.py             CognitiveCore (handle_turn, run_mission)
  intent.py           Intent, IntentKind, IntentInterpreter,
                      ModelIntentInterpreter, RuleBasedIntentInterpreter
  selection.py        CapabilitySelector, CapabilityProposal, Selection
  evidence.py         Claim, ClaimKind, EvidenceStore, ClaimGuard
  respond.py          ResponseComposer, UserReply
  reflect.py          Reflector
  replan.py           ReplanController, ReplanDecision
  brief.py            SelfBrief (view del SelfModel para cognición)
  envelope.py         EnvelopeBuilder
  planner_model.py    ModelPlanner, PlanValidator  (planner.py sigue con RuleBased)
alexis/models/
  provider.py         ModelTask, ModelRequest, ModelResponse, ModelProvider
  router.py           (evuelve el stub actual) ModelRouter con routing+fallback
  degraded.py         DegradedModel / EchoModel (determinista, offline)
  providers/
    local_http.py     adapter HTTP a servidor local (Ollama/vLLM/llama.cpp)
    openai_compatible.py
    omniroute.py      adapter preparado, available=False (honesto)
alexis/memory/
  provider.py         MemoryProvider, MemoryQuery, MemoryItem, MemoryContext
  inprocess.py        InMemoryProvider (envuelve InMemoryMemory)
  postgres.py         PostgresMemoryProvider (observations/missions/verifications)
  null.py             NullMemoryProvider
  obsidian.py         stub prepared, available=False
alexis/conversation/
  session.py          ConversationSession, Turn
  history.py          ConversationHistory (inprocess, luego DB)
```

`planner.py` (F1) **se conserva** como `RuleBasedPlanner`: es el suelo determinista y la
garantía de que el Core nunca queda sin plan.

---

## 3. Contratos

Todos en `alexis/contracts.py` (o módulos nuevos, pero sin duplicar tipos).

```python
class IntentKind(str, Enum):
    GREETING = "greeting"                 # "Hola ALEXIS" → NO mission
    SMALL_TALK = "small_talk"             # NO mission
    SELF_QUERY = "self_query"             # "¿qué hiciste?" → SelfModel, NO mission
    CAPABILITY_QUERY = "capability_query" # "¿qué puedes hacer?" → catálogo, NO mission
    META_QUERY = "meta_query"             # "¿cómo funcionas?" → NO mission
    TASK = "task"                         # única kind que crea Mission
    UNKNOWN = "unknown"

@dataclass
class Intent:
    kind: IntentKind
    utterance: str
    objective: str | None = None            # solo TASK
    target: str | None = None               # archivo, proyecto, url...
    success_criteria: list[str] = field(default_factory=list)
    requested_capabilities: list[str] = ... # lo que el modelo SUGIERE (no es permiso)
    side_effects_intent: str = "unknown"    # read | modify | delete | external | unknown
    ambiguity: str | None = None
    needs_clarification: bool = False
    confidence: float = 0.0
    model_meta: dict = ...                  # provider/model/latency/cost/fallback

@dataclass
class SelfBrief:
    identity: dict
    state: str
    goal: str | None
    mission_id: str | None
    available_capabilities: list[str]
    required_capabilities: list[str]
    missing_capabilities: list[str]         # required - available
    permissions: dict
    envelope: dict
    context: list[str]
    uncertainties: list[str]
    confidence: float | None
    pending_approvals: list[dict]
    recent_actions: list[dict]
    dependencies: list[str]
    limits: list[str]

@dataclass
class MemoryQuery:
    text: str
    mission_id: str | None = None
    kinds: list[str] = field(default_factory=lambda: ["observations", "episodic", "semantic"])
    limit: int = 10
    token_budget: int = 4000

@dataclass
class MemoryItem:
    id: str
    kind: str          # observations | episodic | semantic | procedural
    content: str
    source: str
    score: float
    mission_id: str | None = None
    created_at: str | None = None

@dataclass
class MemoryContext:
    items: list[MemoryItem]
    sources: list[str]
    token_estimate: int
    provider: str

class ClaimKind(str, Enum):
    FACT = "fact"
    EVIDENCE = "evidence"
    INFERENCE = "inference"
    UNCERTAINTY = "uncertainty"
    ASSUMPTION = "assumption"

@dataclass
class Claim:
    id: str
    kind: ClaimKind
    text: str
    source: str                        # tool / mission / memory / model
    evidence_ids: list[str] = field(default_factory=list)
    confidence: float = 0.0
    verified: bool = False

@dataclass
class CapabilityProposal:
    capabilities: list[str]
    rationale: str
    model_meta: dict

@dataclass
class Selection:
    selected: list[str]
    rejected: list[dict]               # {capability, rule, reason}
    rationale: str

@dataclass
class ReplanDecision:
    action: str          # retry_same | retry_alternative | ask_user | abort
    alternative_capability: str | None
    reason: str
    attempt: int

@dataclass
class UserReply:
    text: str                          # natural (experiencia principal)
    kind: str                          # answer | question | approval_request | error
    claims: list[Claim]
    evidence: list[dict]
    open_questions: list[str]
    mission_id: str | None
    self_update: dict
```

**Invariantes de contrato**

- `MissionEnvelope` (F1) no cambia: sigue siendo la declaración de permisos. F2 no
  añade permisos; los usa.
- `PlanStep.capability` (F1) es obligatorio en F2 para pasos que ejecutan; el
  validator rechaza pasos sin capability resoluble.
- `ExecutionResult` / `Observation` / `Verification` (F0) se conservan; F2 añade
  `Verification.checks` y `Verification.claim_ids` como campos opcionales
  (retrocompatible: default_factory).

---

## 4. Model Provider (interfaz intercambiable)

`cognition.understand` y `cognition.analyze` dejan de ser deterministas: pasan a ser
*tareas de modelo*. El Core no cambia nunca; cambia el provider registrado.

```python
class ModelTask(str, Enum):
    UNDERSTAND = "understand"     # intención
    ANALYZE = "analyze"           # análisis de contexto/evidencia
    REASON = "reason"             # razonar sobre opciones
    SELECT = "select"             # proponer capabilities
    PLAN = "plan"                 # plan dinámico
    SYNTHESIZE = "synthesize"     # respuesta natural
    CRITIQUE = "critique"         # verificar conclusiones
    REFLECT = "reflect"           # lección/reflexión

@dataclass
class ModelRequest:
    task: ModelTask
    system: str
    messages: list[dict]                 # [{role, content}]
    schema: dict | None = None           # JSON Schema para salida estructurada
    max_tokens: int = 1500
    temperature: float = 0.2
    privacy: str = "normal"              # normal | sensitive | secret ( routing )
    max_cost_usd: float = 0.0
    deadline_ms: int = 30000

@dataclass
class ModelResponse:
    text: str
    data: dict | None                    # salida estructurada validada
    provider: str
    model: str
    outcome: ModelOutcome = ModelOutcome.REAL   # REAL | DEGRADED | UNAVAILABLE (P1)
    fallback_used: bool = False
    fallback_from: str | None = None     # provider que falló antes
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    error: str | None = None
    chain: list[str] = field(default_factory=list)   # providers intentados

class ModelOutcome(str, Enum):
    REAL = "real"                # model output real de un provider real
    DEGRADED = "degraded"        # contingencia determinista (DegradedModel)
    UNAVAILABLE = "unavailable"  # no existe provider utilizable

class ModelProvider(ABC):
    id: str
    supports: set[ModelTask]
    priority: int                        # menor = preferido
    cost_per_1k_tokens: float
    latency_p50_ms: int
    privacy_max: str                     # hasta qué nivel de privacidad llega
    available: bool                      # si no, el router lo ignora (honesto)
    degraded: bool = False               # True solo para contingencia determinista

    @abstractmethod
    async def complete(self, request: ModelRequest) -> ModelResponse: ...
```

**Regla P1**: `outcome` es la única fuente de verdad sobre la procedencia del
razonamiento. `provider.degraded=True` ⇔ `ModelResponse.outcome=DEGRADED` ⇔
`model.routed.outcome="degraded"` ⇔ nota de procedencia en la respuesta final. Nunca
se degrada en silencio, y `UNAVAILABLE` no se disfraza de `DEGRADED`.

### 4.1 Providers de F2

| Provider | Estado F2 | Notas |
|---|---|---|
| `local_http` (Ollama/vLLM/llama.cpp) | implementado | por defecto si `ALEXIS_MODEL_BASE_URL` está definido |
| `openai_compatible` | implementado | cualquier endpoint compatible (cloud o local) |
| `degraded` / `echo` | implementado | determinista, offline, para tests y fallback final |
| `omniroute` | **adapter preparado, `available=False`** | igual que la capability `omniroute.run` (missing) |

Configuración (sin tocar el Core):

```bash
ALEXIS_MODEL_PROVIDER=local_http|openai_compatible|omniroute|none
ALEXIS_MODEL_BASE_URL=http://127.0.0.1:11434
ALEXIS_MODEL_NAME=qwen2.5-coder
ALEXIS_MODEL_API_KEY=...
ALEXIS_MODEL_FALLBACK=degraded
```

---

## 5. Model Router

Evoluciona el stub actual `alexis/models/router.py` (hoy `ModelRequest.task: str`,
`complete()` vacío). El Router **es** el punto único por el que el Core pide
cognición.

```python
class ModelRouter:
    def register(self, provider: ModelProvider) -> None
    def unregister(self, provider_id: str) -> None
    def candidates(self, request: ModelRequest) -> list[ModelProvider]  # orden determinista
    async def complete(self, request: ModelRequest) -> ModelResponse:
        chain = self.candidates(request)        # disponible + soporta la tarea
        for p in chain:
            try:
                resp = await asyncio.wait_for(p.complete(request), request.deadline_ms/1000)
                if resp.error:
                    raise ModelProviderError(resp.error)
                resp.chain = [tried..., p.id]
                return resp                     # outcome=REAL (o DEGRADED si p.degraded)
            except Exception:
                continue                         # fallback al siguiente
        if self.allow_degraded and self._degraded_provider() is not None:
            return await self._degraded_provider().complete(request)  # outcome=DEGRADED
        return ModelResponse(                    # outcome=UNAVAILABLE
            text="", data=None, provider="none", model="none",
            outcome=ModelOutcome.UNAVAILABLE, fallback_used=True,
            error=last_error or "sin provider utilizable para la tarea",
        )
```

Criterios de routing (en este orden, documentados):

1. `supports` contiene la tarea.
2. `available` es verdadero.
3. `privacy` de la tarea cabe en `privacy_max` del provider.
4. presupuesto: providers de pago que exceden el budget restante se descartan.
5. orden: `priority`, luego `cost_per_1k_tokens`, luego latencia; los que no caben en
   `deadline_ms` van al final (se intentan, no se prometen).
6. Fallback: siguiente provider de la cadena.
7. Si se agota la cadena: `DegradedModel` **solo si está permitido**
   (`allow_degraded`, config `ALEXIS_MODEL_FALLBACK`) → `outcome=DEGRADED`.
8. Si tampoco hay contingencia permitida: `outcome=UNAVAILABLE` con el error. El Router
   **no lanza excepciones** al Core por falta de provider: devuelve el estado.

**Separación estricta (P2)**: el Router **no decide qué hay que hacer** (eso es el
Cognitive Core) ni **qué se puede hacer** (eso es Policy/Envelope). Solo resuelve
*qué proveedor puede satisfacer una tarea ya decidida*. No conoce el catálogo de
capabilities ni el envelope. Cambiar de provider es registro/config, nunca una edición
del Core. OmniRoute, cuando exista, es un provider más: si el Core dependiera de él,
esto sería una violación de P2.

El Router registra en eventos: `model.routed` (task, provider, model, outcome, latency,
cost, fallback, chain, error) → alimenta experiencia, Self Model (coste/latencia) y
auditoría. `outcome` va siempre explícito, incluido en respuestas degradadas/no
disponibles.

---

## 6. Memory Provider

El Core pregunta: *«¿Qué contexto relevante tengo para esta solicitud?»* y recibe
contexto estructurado. No implementamos semántica completa ni Obsidian, pero la
interfaz queda lista.

```python
class MemoryProvider(ABC):
    id: str
    available: bool

    @abstractmethod
    async def retrieve(self, query: MemoryQuery) -> MemoryContext: ...

    @abstractmethod
    async def store_observation(self, mission_id: str, obs: Observation, claims: list[Claim] | None = None) -> None: ...

    @abstractmethod
    async def store_experience(self, record: dict) -> None: ...   # episodic/semantic

class NullMemoryProvider(MemoryProvider):     # siempre disponible, vacío
class InMemoryProvider(MemoryProvider):       # envuelve el InMemoryMemory actual
class PostgresMemoryProvider(MemoryProvider): # observations + missions + verifications (ya existen)
class ObsidianMemoryProvider(MemoryProvider): # prepared, available=False
```

Proveedores F2: `InMemoryProvider` (por defecto), `PostgresMemoryProvider` (con
repositorios existentes), `NullMemoryProvider`, stub `ObsidianMemoryProvider`.
Observaciones van a la tabla `observations` (ya existe) y `claim`s como
`Observation(source="claim")`; la tabla `claims` queda propuesta para F3.

El Core **usa** la memoria antes de decidir (contexto) y escribe al terminar
(observaciones + experiencia). Esto convierte la memoria en parte real de la
cognición, no un adorno.

---

## 7. Self Model en la cognición (requisito 4)

El Self Model de F1 (`alexis/self/model.py`, 23 zonas) pasa de "se actualiza por
eventos" a "se consulta antes de actuar y se actualiza después":

1. **Antes de actuar**: `SelfModel.brief() -> SelfBrief` expone las 14 zonas que el
   Core necesita (identidad, estado, objetivo, misión, capacidades disponibles,
   permisos, envelope, contexto, incertidumbre, aprobaciones pendientes, acciones
   recientes, dependencias, límites, required/missing capabilities). El brief entra al
   prompt del modelo y a la selección de capabilities.
2. **Durante**: el Core escribe (`record_decision`, `record_capability_use`,
   `record_evidence`, `record_observation`) en el SelfModel, no solo el bus.
3. **Después**: `SelfModel.apply_outcome(...)` (éxito/fallo/replan/reflexión/
   lección) y `SelfModelSync` sigue respetando el bus como fuente de presencia.
4. **Gaps**: `SelfBrief.missing_capabilities` alimenta la decisión "no puedo hacer X"
   → el planner nunca propone una capability ausente, y la respuesta lo dice.

El SelfModel deja de ser un read-model de UI: pasa a ser el estado consultable que
conditiona planificación, selección y respuesta. Si el SelfModel está vacío o
desactualizado, el Core lo nota (`self_update` en el reply, `observations_about_self`).

Regla: la IA no reescribe el SelfModel a ciegas; el Core escribe hechos verificados
(resultados, policy decisions, evidencia), y el modelo solo propone texto de síntesis.

---

## 8. Conversación como puerta principal (requisito 3)

`ConversationSession` reemplaza `POST /missions` como entrada del usuario. `/missions`
se conserva (API/test), pero es secundaria.

```python
class ConversationSession:
    def __init__(self, *, core: CognitiveCore, self_model, bus, history):
        ...

    async def handle_turn(self, utterance: str) -> UserReply:
        # 1. publica conversation.turn_started  (presence: listening → thinking)
        # 2. brief = self_model.brief()
        # 3. intent = core.intent.interpret(utterance, brief, history)
        # 4. si intent.kind != TASK:
        #       respuesta directa desde SelfModel/catálogo/claims (SIN Mission)
        # 5. si intent.kind == TASK:
        #       envelope = core.envelope_builder.build(intent, brief, registry)
        #       mission = MISSIONS.create(intent.objective, envelope)
        #       reply = await core.run_mission_cognitive(mission)
        # 6. publica conversation.turn_finished; devuelve UserReply
```

Mapeo de los ejemplos del enunciado:

| Usuario | Intent | Mission? | Camino |
|---|---|---|---|
| "Hola ALEXIS." | `GREETING` | **no** | respuesta natural del modelo, sin core de misión |
| "¿Qué puedes hacer?" | `CAPABILITY_QUERY` | **no** | `CapabilitySelector.describe()` + SelfBrief + claims honestas (habilitadas vs `missing`) |
| "Revisa este proyecto." | `TASK` | **sí** | pipeline completo (ver §9) |

`RuleBasedIntentInterpreter` (determinista) es el fallback cuando el modelo no está
disponible o la confianza < umbral: clasifica por keywords/reglas (saludo, pregunta
"qué puedes", imperativo) para que el Core **nunca** quede mudo. El modelo
(`ModelIntentInterpreter`) refina; valida el kind y, si propose `TASK`, extrae
objective/target/criteria.

---

## 9. Flujo F2 de extremo a extremo

`CognitiveCore.handle_turn(TASK)`:

```
1  turn_started                        presence=thinking
2  intent = ModelIntent(...).interpret          # modelo
3  brief = SelfModel.brief()                    # Self Model F1
4  ctx   = MemoryProvider.retrieve(MemoryQuery) # memoria
5  reason = Router.complete(REASON, [utterance, brief, ctx])  # razonamiento
6  proposal = Router.complete(SELECT, [reason, catálogo])      # model propone caps
7  selection = CapabilitySelector.select(proposal, envelope, policy, catalog)
        # descarta no habilitadas / fuera de envelope / excessive riesgo (rechaza con regla)
8  plan  = ModelPlanner(...).create_plan(intent, selection, ctx, brief)
        # valida con PlanValidator; si inválido → RuleBasedPlanner.create_plan
9  envelope = EnvelopeBuilder.build(intent, brief, selection)   # permisos, NO lo da el modelo
10 mission = MISSIONS.create(...) + cola FIFO (F1)
11 for step in plan (DAG):
       decision = AutonomyGate.decide(...)   # F1: policy + approval
       events: policy.evaluated, mission.step_evaluated   # presencia=evaluating
       if requires_approval: WAITING_APPROVAL → presence=waiting_for_approval → return
       result = SandboxExecutor/Execution.execute(step)   # F1
       obs = Observations(result)                        # presencia=working
       claims = EvidenceStore.from_result(...)           # EVIDENCE
       if not result.success:
            decision = ReplanController.on_failure(...)   # presencia=replanning/recovering
            if action in (retry_same, retry_alternative): goto step alternative (bucle, cota)
            else: ask_user / abort                       # presencia=warning/error
       eval = Router.complete(ANALYZE/REASON, [obs, claims])   # presencia=evaluating
       claims += EvidenceStore.from_reasoning(eval)            # INFERENCE/ASSUMPTION
12 verification = Verifier.verify(mission, plan) + ModelCritic   # presencia=verifying
       # deterministas primero; el crítico NO puede pasar solo
       if not verification.passed:
            ReplanController (cota) | ask_user
13 reflect = Reflector.reflect(mission, claims, verification)   # presencia=reflecting
       lessons → SelfModel
       experience → MemoryProvider.store_experience
14 reply = ResponseComposer.compose(intent, claims, verification, reflect)  # presencia=speaking
15 SelfModel.apply_outcome(...) + MemoryProvider.store (obs/claims/experience)
       presence=success | warning | error
16 return UserReply (texto natural + claims/evidence structured)
```

Punto clave: **el modelo propone; F1 autoriza**. En el paso 7 y 9 el modelo no puede
otorgarse permisos: `CapabilitySelector` y `EnvelopeBuilder` recortan contra catálogo y
envelope, y `AutonomyGate`/`PolicyEngine` (F1) vuelven a evaluar cada paso.

---

## 10. Capability Selection (requisito 7)

No hay lista fija de herramientas por tipo de misión. El selector:

1. **Consulta el catálogo** (F1 `CapabilityRegistry`) con filtros declarativos
   (`sphere`, `side_effects`, `network`, `default_risk`, `requires_input`,
   `sandbox_profile`, `status`, `enabled`) — el Core no codifica "para revisar proyecto
   uso X"; pregunta al catálogo qué puede servir.
2. El **modelo propone** un subconjunto (`CapabilityProposal`).
3. `CapabilitySelector.select()` recorta: `enabled ∧ ⊆ envelope ∧ policy(cap) != deny`.
   Lo rechazado se registra con `rule` (auditoría) y puede formar un `INFERENCE`/límite
   en la respuesta.
4. Lo que sobra o falta se explica con `rationale` (visible para el usuario en texto
   natural, no JSON crudo).

Regla dura: `policy.evaluated` con `verdict=deny` **gana** a cualquier propuesta del
modelo.

---

## 11. Planner dinámico (requisito 6)

`ModelPlanner` genera un DAG de **etapas** (no una plantilla). Cada paso declara
`capability`, `depends_on`, `risk`, `requires_input`, y opcionalmente `verification`.
Etapas reutilizadas del runtime: `understand|recall|inspect|analyze|plan|modify|test|
execute|verify|synthesize|respond|replan`.

Los ejemplos del enunciado seexpressed como **resultado esperado** del planner (no
como rutas hardcodeadas):

| Objetivo | Plan esperado (típico) |
|---|---|
| "¿Qué es X?" | `understand → respond` |
| "Investiga X." | `understand → research → evaluate → respond` |
| "Revisa mi proyecto." | `understand → inspect(fs.read/fs.stat) → analyze → verify → respond` |
| "Corrige este error." | `understand → inspect → plan → [approval] → modify → test → verify → respond` |

`PlanValidator` (determinista) antes de aceptar un plan del modelo:

- cada `capability` ∈ selección ∧ habilitada ∧ (envelope vacía ∨ ⊆ envelope);
- DAG acíclico y `depends_on` resueltos;
- ninguna etapa `respond` sin `synthesize` si hay claims (respuesta con evidencia);
- `requires_approval` coherente con `CapabilitySpec.default_risk` y envelope
  (`approval_required`, `auto_approve`);
- las etapas `modify|remove|commit` exigen capability con `side_effects=True`.

Si el plan del modelo falla la validación → se registra `plan.invalid` (con reasons) y
se usa `RuleBasedPlanner` (suelo determinista). El Core **nunca** ejecuta un plan no
validado.

Persistencia: `plan_to_dict/from_dict` (F1) se extienden con `requires_input`,
`verification`, `proposed_by`; los missions con plan F1 (sin campos nuevos) siguen
cargando (retrocompatible).

---

## 12. Evidence (requisito 8)

```python
class EvidenceStore:
    def from_observation(obs) -> list[Claim]         # EVIDENCE (o FACT si verificado)
    def from_verification(v)    -> list[Claim]       # FACT (verificado)
    def from_reasoning(model)   -> list[Claim]       # INFERENCE/ASSUMPTION/UNCERTAINTY
    def attach(claim, evidence_ids)                  # trazabilidad
    def claims_for_mission(mission_id) -> list[Claim]
```

Reglas (`ClaimGuard`, deterministas):

- un `FACT` exige ≥1 `EVIDENCE` y `verified=True`; si no → se degrada a `INFERENCE`
  (o `UNCERTAINTY` si no hay evidencia) **antes** de llegar al texto final.
- `ASSUMPTION` se declara explícitamente (no se esconde).
- el compositor de respuesta solo puede usar claims persistidos; no puede introducir
  afirmaciones nuevas sin evidencia → si el modelo alucina, cae a `UNCERTAINTY`.
- la fuente de todo claim queda registrada (`source`, `evidence_ids`) → auditables.

`EVIDENCE` = observation de herramienta. `FACT` = verificado por verificador
determinista. `INFERENCE` = razonamiento del modelo. `UNCERTAINTY` = reconocido como no se sabe.
`ASSUMPTION` = supuesto declarado (p.ej. "asumo que el workspace es el proyecto").

En F2, claims se persisten como `Observation(source="claim", content=Claim)` (tabla
`observations` existente) y se recuperan por mission; una tabla `claims` se propone para
F3 (índices por tipo/origen).

---

## 13. Verificación (requisito 9)

- `Verification` (F0) se mantiene como resultado canónico; F2 añade campos opcionales:
  `checks: list[CheckResult]`, `claim_ids: list[str]`, `independent: bool`.
- `Verifier` existente (`FilesystemVerifier`) sigue siendo la fuente de `FACT`
  determinista. Se añaden verificadores por dominio cuando exista la capability
  (git diff, tests, etc., hoy `missing`).
- `ModelCriticVerifier` (modelo como crítico) **no puede** poner `passed=True` solo:
  el veredicto final exige al menos un check determinista `passed`; el crítico añade
  `INFERENCE`/`UNCERTAINTY` y puede **bajar** la confianza.
- Si `verification.failed` → `ReplanController` (cota) o `ask_user`; el Core nunca
  declara éxito por opinion del modelo.

---

## 14. Respuesta natural (requisito 10)

`ResponseComposer.compose(intent, claims, verification, reflection) -> UserReply`:

- `text` es la experiencia principal: párrafos en lenguaje natural que cubren **qué
  entendí, qué hice, qué encontré, con qué evidencia, con qué incertidumbre, qué
  necesito de ti, resultado final**.
- `claims`/`evidence`/`open_questions` quedan como datos estructurados (debug, UI,
  auditoría, tests), no como respuesta al usuario.
- El compositor es determinista en su estructura y usa el modelo para la redacción; si
  el modelo degrada, hay una plantilla honesta que igualmente lista hechos/evidencia.
- `apps/demo` y Face muestran `text`; `experience/presenter.py` evoluciona para
  consumir `UserReply` (manteniendo `/state` para debug).

`Reflector` (pregunta "¿qué aprendí?") produce `lessons` → SelfModel + memoria
episódica; alimenta `capabilities` candidatas futuras sin auto-otorgarlas.

---

## 15. Estados, eventos y Presence (requisito 11)

Eventos semánticos nuevos (topic, payload mínimo):

```
conversation.turn_started      {turn_id, source}
conversation.turn_finished     {turn_id, kind, mission_id?}
conversation.clarification     {turn_id, question}
cognition.intent_understood   {turn_id, kind, confidence, model_meta}
cognition.self_consulted      {turn_id, brief_zones: [...]}
cognition.context_retrieved   {turn_id, items, sources, token_estimate}
cognition.reasoned            {turn_id, summary_len, model_meta}
cognition.capabilities_selected {turn_id, selected, rejected}
cognition.plan_created        {turn_id, steps: [id, capability]}
cognition.plan_invalid        {turn_id, reasons}
cognition.plan_revised        {turn_id, attempt}
cognition.claims_formed       {mission_id, counts: {kind: n}}
model.routed                   {task, provider, model, latency_ms, cost_usd, fallback, degraded}
evidence.formed               {mission_id, claim_ids}
verification.finished         {mission_id, passed, confidence, independent}
reflection.recorded           {mission_id, lesson}
conversation.reply_composed    {turn_id, kind, claims: n, degraded}
```

Mapeo a Presence (16 estados de F0-Self). El Core publica la fase **explícita**
(la presencia refleja lo que el Core está haciendo, no lo que adivinamos):

| Presence | Evento que lo origina |
|---|---|
| `listening` | `conversation.turn_started` (o `presence.listening`) |
| `thinking` | `cognition.intent_understood`, `cognition.self_consulted` |
| `researching` | `cognition.context_retrieved` (con items) |
| `planning` | `cognition.plan_created` |
| `evaluating` | `cognition.reasoned`, `policy.evaluated`, `cognition.claims_formed` |
| `working` | `mission.step_started` (ejecución real de tool) |
| `replanning` | `cognition.plan_revised`, `mission.replanning` |
| `recovering` | fallo retriable en curso |
| `waiting_for_approval` | `mission.approval_required` |
| `verifying` | `verification.finished` (en curso) |
| `reflecting` | `reflection.recorded` |
| `speaking` | `conversation.reply_composed` / `presence.speaking` |
| `success` | turno/misión completada con verificación pasada |
| `warning` | misión con incertidumbre no resuelta o fallback de modelo |
| `error` | turno fallido / abortado honestamente |

`SelfModelSync` (F0) ya deriva presencia y soporta flags transitorios; F2 lo extiende
para consumir las fases explícitas (`cognition.*`) sin reescribir la derivación
(`derive_presence(flag=...)`). El Face `CoreStateAdapter` deja de adivinar y lee la
presencia real (cierra F0-Presence pendiente).

---

## 16. Seguridad: invariantes que NO cambian

1. El **modelo no otorga permisos**: propone; `CapabilitySelector` + `EnvelopeBuilder`
   recortan; `PolicyEngine`/`AutonomyGate` (F1) autorizan; nada auto-aproba.
2. Una **misión no excede su envelope** (capability fuera → `deny`; F1).
3. **Credenciales = referencias**, nunca en contexto por defecto.
4. **Contenido no confiable = datos, no instrucciones** (defensa ante prompt
   injection): observations/memoria entran al prompt marcados como datos, nunca como
   órdenes del Core.
5. Operaciones de alto impacto → `approval` salvo `auto_approve` explícito.
6. **Verificación independiente** para operaciones consecuentes (determinista primero).
7. Auditoría de cada decisión (`policy.evaluated`, `model.routed`, `plan.invalid`).
8. Más autonomía ⇒ más evidencia y más verificación.
9. **Honestidad epistémica**: `ClaimGuard` impide presentar `INFERENCE` como `FACT`;
   `degraded=True` se propaga y se dice.
10. **Bote de coste/latencia**: envelope `max_cost_usd` + `deadline_ms`; Router
    respeta presupuesto; si se agota → degradar/abortar, no exceder.

---

## 17. Plan de fases de F2 (con puertas)

Cada fase es pequeña, verificable y no rompe lo anterior.

| Fase | Contenido | Puerta (tests) | Estado |
|---|---|---|---|
| **F2.0** Contratos + Evidence | `Intent/SelfBrief/MemoryQuery/Claim/Selection/…`, `ClaimKind`, extend `Verification` | tests de contratos + 130 verdes | **hecho** (29 tests) |
| **F2.1** Model layer | `ModelProvider`, `ModelRouter` (routing+fallback+budget), `local_http`/`openai_compatible`/`degraded`, `omniroute` stub | `test_model_router.py` + test "swap provider sin tocar Core" | **hecho** (33 tests) |
| **F2.2** Memory Provider | interfaz + `InMemory`/`Postgres`/`Null`/`Obsidian` stub | `test_memory_provider.py` | pendiente |
| **F2.3** Conversación + intención | `ConversationSession`, `ModelIntent` + `RuleBased` fallback, `EnvelopeBuilder`, endpoints `/chat` | AC1/AC2 (saludo sin mission; capacidades reales) | pendiente |
| **F2.4** Self en cognición | `SelfModel.brief()` + mutadores + `missing_capabilities` | AC7 (consulta antes, actualiza después) | pendiente |
| **F2.5** Selección + planner dinámico | `CapabilitySelector`, `ModelPlanner` + `PlanValidator`, fallback `RuleBased` | AC3/AC11 (plan ≠ plantilla fija; sin escalada) | pendiente |
| **F2.6** Evidence + verificación + respuesta | `EvidenceStore`/`ClaimGuard`, `ModelCritic`, `ResponseComposer`, `Reflector` | AC5/AC6/AC10 | pendiente |
| **F2.7** Replanning + Presence + E2E | `ReplanController`, eventos `cognition.*`, escenario principal | AC1–AC13 verdes | pendiente |

Las 130 pruebas existentes (114 F0 + 9 Self + 7 Capabilities) se mantienen verdes en
todas las fases (`/missions` y `AlexisRuntime.run_mission` se preservan como camino
legacy; el camino cognitivo es adicional).

---

## 18. Pruebas de comportamiento

### 18.1 Unitarias / de contrato

- `test_cognitive_core.py`:
  - turno `GREETING` → **ninguna** misión creada (delta de misiones = 0);
  - `CAPABILITY_QUERY` → respuesta cita ≥1 capability habilitada y menciona alguna
    `missing` de forma honesta; no inventa;
  - `TASK` "revisa este proyecto" → plan con ≥3 capacidades distintas, **no** igual a
    la plantilla fija de 4 etapas, y cada `step.capability` habilitada y ⊆ envelope;
  - SelfBrief consultado antes de cada decisión de selección (spy).
- `test_model_router.py`: routing por tarea; cadena de fallback (provider A falla → B →
  degraded); presupuesto; deadline; swap de provider **solo por registro/config**.
- `test_memory_provider.py`: `retrieve` devuelve `MemoryContext` estructurado; `Null`
  vacío y válido; `Postgres` usa `observations`.
- `test_evidence.py` (ClaimGuard): `FACT` sin evidencia → degradado; `ASSUMPTION`
  explícita; inferencia no se presenta como hecho.
- `test_selection.py`: propuesta fuera de envelope/habilitada → rechazada con `rule`;
  propuesta válida → seleccionada; catálogo filtrado (no lista fija).
- `test_replan.py`: fallo → replan (alternativa dentro de cota); excede cota → `ask_user`;
  no loop infinito.
- `test_conversation.py` (extiende Self): el Core no muta permisos con el modelo
  (intento de auto-aprobar `fs.remove` → `deny`).

### 18.2 End-to-end (escenario principal)

`"ALEXIS, revisa este proyecto y dime qué problemas importantes encuentras."`
(sobre un workspace con problemas sembrados). Assertions (no secuencia exacta, sino
propiedades):

1. Se creó 1 misión (kind TASK) con plan dinámico ≠ plantilla fija.
2. Al menos 2 capabilities distintas del workspace/inspección usaron **tools reales**
   (evidence `source=tool`).
3. Claims: todo `FACT` de la respuesta tiene ≥1 `EVIDENCE`; los problemas "importantes"
   son `INFERENCE` o `FACT` con evidencia, nunca inventados.
4. Verificación determinista pasó (o, si no, hubo replan o `ask_user` honesto).
5. `reply.text` es natural, no JSON; menciona incertidumbre si la hay.
6. Eventos de Presence emitidos y coherentes con el estado real (incl. `evaluating`,
   `verifying`, `reflecting`).
7. SelfModel actualizado (lecciones/claims/acciones) y recuperable en `/self`.
8. Cambiar el provider (echo→local, o disponible→no disponible) **no** cambia
   `CognitiveCore` ni las rutas de seguridad; degrada honestamente.

### 18.3 Determinismo / no-regresión

- Con `degraded`/determinista y semilla fija → mismo plan y misma respuesta.
- Las 130 pruebas previas siguen verdes (incl. las de autonomy/policy/gates/queue).
- Tests de seguridad: escalada de permisos, prompt-injection en observations,
  credenciales fuera de contexto, presupuesto.

---

## 19. Criterios de aceptación

| # | Criterio | Verificación |
|---|---|---|
| AC1 | "Hola ALEXIS" no crea misión | test + log |
| AC2 | "¿Qué puedes hacer?" responde desde Self Model + catálogo, con estado honesto (habilitadas y faltantes) | test |
| AC3 | "Revisa este proyecto" produce plan **dinámico** (≠ understand→research→execute→verify), con capabilities del catálogo | test |
| AC4 | El modelo participa en ≥6 funciones cognitivas (understand, analyze/reason, select, plan, synthesize, critique/reflect) | contador de `model.routed` por turno |
| AC5 | Respuesta: cada `FACT` con evidencia; nada de inferencia como hecho | ClaimGuard + test |
| AC6 | Fallo de un paso → replan acotado o `ask_user` (nunca éxito falso) | test |
| AC7 | SelfModel consultado antes de actuar y actualizado después | eventos + test |
| AC8 | Cambiar de provider no toca el Core ni las rutas de seguridad | test de config |
| AC9 | Presence refleja fases reales (no simuladas); eventos semánticos emitidos | eventos + test |
| AC10 | Verificación precede a "completado"; éxito solo con verificación pasada (o `warning` honesto) | test |
| AC11 | El modelo no puede otorgar permisos; `deny` de policy gana | test de escalada |
| AC12 | 130 tests previos verdes + nuevos verdes; determinismo con provider determinista | suite |
| AC13 | Interfaz principal = texto natural; JSON queda interno/debug | inspección `/chat` + UI |

---

## 20. Riesgos

- **Alucinación de capacidades**: mitigado con `ClaimGuard` + selection recortada +
  honestidad `degraded`.
- **Provider caído/lento**: Router fallback + deadline + degradación honesta.
- **Coste**: envelope `max_cost_usd` + budget del Router; abortar si se excede.
- **Replan loops**: cota `max_replans` (por defecto 2) y `ask_user` como salida.
- **Scope**: F2 no abre C2–C5; si el escenario principal necesita leer un proyecto
  fuera del workspace, seguirá limitado al perímetro (honesto) hasta C2/C3.
- **Colisión de nomenclatura** F2 (arreglada en §0 al aprobar).

---

## 21. Archivos existentes que deberán evolucionar

(Detalle en la respuesta al usuario; resumen aquí.)

| Archivo | Evolución F2 |
|---|---|
| `alexis/contracts.py` | + `Intent/IntentKind, SelfBrief, MemoryQuery/MemoryItem/MemoryContext, Claim/ClaimKind, CapabilityProposal/Selection, ReplanDecision, UserReply`; `Verification` + campos opcionales; `PlanStep` + `requires_input/verification/proposed_by` (opcionales) |
| `alexis/models/router.py` | stub → `ModelRouter` real (routing por tarea, fallback, budget, deadline); conserva `complete()` |
| `alexis/models/provider.py` *(nuevo)* | `ModelTask/ModelRequest/ModelResponse/ModelProvider` |
| `alexis/models/providers/*` *(nuevos)* | `local_http`, `openai_compatible`, `omniroute` (stub honesto), `degraded` |
| `alexis/memory/store.py` | `MemoryStore` se mantiene; `MemoryProvider` (nuevo) lo envuelve/expande para cognition; `InMemoryMemory` cumple ambos contratos |
| `alexis/memory/provider.py` *(nuevo)* | `MemoryProvider/Query/Item/Context` + `InMemory/Postgres/Null/Obsidian` |
| `alexis/cognition/planner.py` | conserva `RuleBasedPlanner`; añade firma `create_plan(mission, understanding=None)`; `plan_to_dict/from_dict` extendidos (retrocompatibles) |
| `alexis/cognition/planner_model.py` *(nuevo)* | `ModelPlanner` + `PlanValidator` |
| `alexis/cognition/core.py` *(nuevo)* | `CognitiveCore.handle_turn/run_mission_cognitive`; orquesta todo §9 |
| `alexis/cognition/{intent,selection,evidence,respond,reflect,replan,brief,envelope}.py` *(nuevos)* | piezas del loop cognitivo |
| `alexis/security/policy.py` + `policy_rules.py` | sin cambio de diseño; se les añade soporte de `capability` ya presente (F1). Posible regla nueva `capability.missing` (usar `missing` de catálogo) |
| `alexis/autonomy/gates.py` | sin cambio de diseño; ya consume `PolicyDecision` granular; quizá `GateDecision` inclúa `claims_checked` |
| `alexis/execution.py` | dispatch por capability (F1 pendiente de hacer: hoy `ACTION_TOOL` fijo) para que `step.capability` resuelva tool; capability ausente → error honesto |
| `alexis/verification.py` | + `checks/claim_ids/independent`; `ModelCriticVerifier` no puede pasar solo; verificadores por dominio cuando exista capability |
| `alexis/self/model.py` | + `brief()`, `record_*`, `apply_outcome`, `missing_capabilities`; sigue siendo fuente de Presence |
| `alexis/self/sync.py` | consumir fases `cognition.*` (flags de presencia) sin reescribir derivación |
| `alexis/core/runtime.py` | `run_mission` sigue (legacy); el camino cognitivo se engancha como colaborador opcional (no se rompe) |
| `alexis/experience/presenter.py` | consumir `UserReply` (texto natural) como fuente principal; mantiene `/state` debug |
| `apps/demo/server.py` | `ConversationSession` + `POST /chat`; `/missions` secundaria; `/self` + `/capabilities` ya; `/conversation/history` |
| `alexis/storage/schema.py` + `repositories.py` | + `conversations/turns`, `claims` (propuesta), índices por `mission_id`/`kind`; reusa `observations` |
| `tests/` | nuevos: `test_cognitive_core`, `test_model_router`, `test_memory_provider`, `test_evidence`, `test_selection`, `test_replan`, `test_conversation`; extender `test_self_model`/`test_capabilities`; las 130 se conservan |

---

## 22. Criterio de éxito (del enunciado, operationalizado)

> "Entiende lo que le estoy pidiendo, decide cómo resolverlo, sabe qué puede hacer,
> sabe qué no puede hacer, utiliza las capacidades disponibles, observa lo que ocurre,
> verifica sus conclusiones y me explica el resultado."

Mapeo a comprobaciones:

| Frase | Mecanismo F2 | Prueba |
|---|---|---|
| "entiende lo que le pido" | `IntentInterpreter` (modelo) | test AC1/AC2 |
| "decide cómo resolverlo" | `Reasoner` + `ModelPlanner` dinámico | AC3/AC4 |
| "sabe qué puede hacer" | catálogo + SelfBrief | AC2 |
| "sabe qué no puede hacer" | `missing_capabilities` + `deny` honesto | AC2/AC11 |
| "utiliza las capacidades disponibles" | `CapabilitySelector` recortada al catálogo | AC3/AC11 |
| "observa lo que ocurre" | `ExecutionResult`→`Observation`→claims | AC5 |
| "verifica sus conclusiones" | verificador determinista + crítico | AC10 |
| "me explica el resultado" | `ResponseComposer` natural + evidencia | AC5/AC13 |

---

**Fin de la especificación F2.** Siguiente paso: revisión del usuario y, tras aprobar,
implementar por fases (§17) empezando por **F2.0 (contratos)** y **F2.1 (model layer)**.
