# P0 — COGNITIVE CORE: GAP ANALYSIS

- Fecha: 2026-09-25
- Commit auditado: `5a48e6b` (Fase 1 + Fase 2.0–2.5 + H1–H3)
- Alcance: los 22 requisitos obligatorios de P0 de `plan_ejecution.md`
- Naturaleza: **solo auditoría**. No se modificó código en este documento.
- Actualización 2026-09-25: implementados **§5.1**–**§5.5**. Ver §7 y §9–§12.
- Veredicto: **P0 = 61.4% de los 22 requisitos del plan — NO está al 100%. P1 no comienza.**
- Los requisitos COMPLETE han pasado de 5 a 8. El porcentaje no se mueve respecto a §5.4
  porque el requisito 23 (ejecución real de tests), que §5.4 añadió, sale del numerador y el
  11 entra; el recuento de requisitos cumplidos sí subió. El 23 no cuenta en los 22 del plan.

---

## 1. Método

Cada requisito se clasifica contra el código real, no contra la intención documentada.

| Veredicto | Significado | Peso |
|---|---|---|
| COMPLETE | Requisito implementado, integrado y con test que lo prueba | 1.00 |
| PARTIAL | Existe parte real; falta integración, cobertura o campo obligatorio | 0.50 |
| BROKEN | Existe implementación pero hace algo que el plan prohíbe | 0.25 |
| MISSING | No existe la funcionalidad | 0.00 |

BROKEN pesa menos que PARTIAL a propósito: un requisito roto es más peligroso que
uno parcial, porque produce falsos positivos (el sistema *parece* cumplirlo).

**Ecuación (sobre los 22 del plan):** `(8×1.00 + 11×0.50 + 0×0.25 + 3×0.00) / 22 = 13.50 / 22 = 61.4%`

*(53.4% original → 54.5% §5.1 (req. 11 de BROKEN a PARTIAL) → 57.3% §5.2 (req. 10 COMPLETE) → 59.1% §5.3 (req. 15 COMPLETE) → 61.4% §5.4 (req. 12 COMPLETE).)*

---

## 2. Clasificación de los 22 requisitos

| # | Requisito | Veredicto | Evidencia |
|---|---|---|---|
| 1 | Intent Engine | PARTIAL | `IntentKind` = greeting, small_talk, self_query, capability_query, meta_query, task, unknown (`alexis/cognition/contracts.py:22`). **Faltan** `question`, `command` y `clarification` como kinds. `MISSION_KINDS={TASK}` correcto: solo las tareas reales crean misión. |
| 2 | Context Assembly | PARTIAL | El runtime entrega conversación, SelfBrief, WorldModel, MemoryProvider, KnowledgeState y envelope al planner (`alexis/core/runtime.py:_ensure_plan`), y Policy se consulta aparte en Gate y Validator. **No existe** un objeto `Context` ensamblado, persistido ni versionado. |
| 3 | Self Model | PARTIAL | Cubre identidad, estado, objetivo, misión, capabilities (con `available`/`required` ya honestos), permisos, envelope, incertidumbres, confianza, fallos, lecciones, decisiones, `answer("next")`. **No tiene** zona de supuestos (vive en KnowledgeState) ni participa en la decisión de autorización: solo consulta capacidades. |
| 4 | World Model | PARTIAL | `alexis/world/model.py` registra lo observado realmente (205 líneas nuevas), `consecutive_failures`, caché de hechos, queries. **Solo en memoria**: dominios FILE y TOOL, sin persistencia, se pierde al reiniciar, no modela tiempo ni resuelve ambigüedades. |
| 5 | Memory | **COMPLETE** | `alexis/memory/provider.py` + `CognitiveRuntime.recall()` antes de `decide()`, y el contenido no confiable se marca como dato, nunca instrucción (`as_prompt_lines()`). Cubierto por `tests/test_memory_provider.py`. |
| 6 | Capability Selection | **MISSING** | No existe capa de selección. El camino determinista usa plantillas fijas; el dinámico existe (`ModelPlanner`) pero está **opt-in y desactivado** porque `llama3.2:1b` no produce planes válidos. El plan exige selección dinámica dependiente del objetivo, explícitamente **no** una secuencia fija. |
| 7 | Dynamic Planning | PARTIAL | `PlanStep` tiene capability, action, args, depends_on, expected, risk y requires_approval. **Faltan dos campos obligatorios**: `objective` por paso (solo existe `description`) y `success_criteria` por paso (`verification` existe pero no se usa). |
| 8 | Execution | **COMPLETE** | `Decision → Policy → Gate → Execution` verificado: `AutonomyGate` sin bypass read-only (H1) y `policy.requires_approval` vinculante en todos los niveles (H3), con `tests/test_approval_authority.py` (14 casos). El modelo no puede concederse permisos. |
| 9 | Observation | **COMPLETE** | Cada invocación produce `ExecutionResult` estructurado, evento en el log, hecho en WorldModel y `Evaluation` que vuelve al loop. |
| 10 | Evaluation | **COMPLETE** | Taxonomía de 5 verdicts implementada y en uso: `Verdict` (`SUCCESS`, `PARTIAL_SUCCESS`, `FAILURE`, `INSUFFICIENT_EVIDENCE`, `BLOCKED`) con contrato documentado; los 9 caminos de retorno de `step()` emiten veredicto explícito y `_settle` lo asienta en el `KnowledgeState` (`tests/test_verdict_taxonomy.py`, 29 tests). |
| 11 | Success Criteria | **COMPLETE** | **Corregido en §5.1.** `MissionEngine.create()` ya no descarta los criterios: acepta `success_criteria` y los persiste en el `Goal` (`alexis/autonomy/mission.py`), y el path con modelo del clasificador los entrega en `apps/demo/server.py:_create_mission_from_intent`. Cubierto por `tests/test_success_criteria_persistence.py` (8 tests, incluido round-trip de persistencia). **Medido desde §5.3 y cerrado en §5.5:** `GoalVerifier` los evalúa con evidencia por criterio, y desde §5.5 la condición de éxito de la misión es `GoalVerification.verified is True`. Es imposible escribir `COMPLETED` sin ella: la invariante está en `Mission.__setattr__` y `settle()` es la única autoridad. |
| 12 | Replanning | **COMPLETE** | **Cerrado en §13.** Firma determinista de la acción, filtro de repetición en producción (`options()`), procedencia por generación y `context_version`, evidencia material con *evidence scope*, y `replan.action_rejected` auditado en `audit_log` + `EventBus` y recuperable tras reinicio. La integración por `run_mission` destapó que el autoparte del sistema (claims `executor` / `observation:tool.*`) invalidaba la guarda: los 24 tests unitarios pasaban y el sistema repetía la lectura. Ver §13.3. Original: el replan funcionaba (diagnóstico → ASK_USER, máx. 2) y está cubierto por tests. **Viola el anti-patrón del plan**: en el MVP repitió la misma lectura con otro id de paso (`read-probe`, `read-probe-2`). No genera una estrategia alternativa; cambia el paso, no la estrategia. |
| 13 | Ask User | PARTIAL | Cubre las causas 1–6 del plan (falta de info, aprobación, fuera de envelope, ambigüedad, capability ausente, incertidumbre). **No hay reanudación**: la respuesta del usuario se descarta y no existe un canal para volver a la misión. |
| 14 | Evidence / Claim Guard | **COMPLETE** | `EvidenceStore` + `ClaimGuard` con 5 tipos; un claim del modelo **nunca** es FACT sin verificación independiente (`alexis/cognition/evidence.py`), con tests de regresión. |
| 15 | Independent Verification | **COMPLETE** | `GoalVerifier` (`alexis/cognition/goal_verification.py`) evalúa el objetivo criterio por criterio contra evidencia de observación real, con estado `satisfied`/`not_satisfied`/`insufficient_evidence`, evidencia asociada y motivo auditable. Exige al menos un criterio y evidencia fiable por criterio: sin criterios no hay verificación. Un claim del modelo nunca basta. `FilesystemVerifier` (nivel de plan) se mantiene como estaba. |
| 16 | Response Composer | **MISSING** | No existe la clase ni el concepto. La respuesta final es una llamada genérica al modelo en `execution.py`; no se compone obligatoriamente de acciones, observaciones, evidencia, verificación, incertidumbre y estado final. |
| 17 | Reflection | **MISSING** | `alexis/learning/system.py` tiene 20 líneas: `ExperienceLearner` solo hace append. **No existe** `outcome → reflection → lesson → experience`. |
| 18 | Persistence | PARTIAL | Persistidos: mission, state, context, plan, current step, observations, claims, approvals, verification, replans, **success_criteria (§5.1, con round-trip probado)**. **Falta `decisions`**: `_record_decision()` solo se invoca desde el camino legacy (`alexis/core/runtime.py:301`), nunca desde el cognitivo. **Falta `reflection`** (no existe aún). |
| 19 | Recovery | PARTIAL | El reinicio recupera la misión y el legacy tiene resume. **No hay test que demuestre** que el loop cognitivo reanuda una misión a mitad de camino conservando WorldModel, memoria y KnowledgeState. |
| 20 | Model Router | **COMPLETE** | Separación estricta Cognitive Core (qué hacer) vs Model Router (qué modelo). Estados REAL / DEGRADED / UNAVAILABLE implementados, incluido el caso de respuesta vacía del proveedor. |
| 21 | Behavioral Tests | PARTIAL | 398 métodos de test, 430 verdes. Cubiertos: greeting sin mission, capability query sin mission, task creando mission, ejecución real, fallo, replanning, approval, missing capability, uncertainty, recovery. **Falta** el test de *false success* a nivel de objetivo: no puede existir porque la capacidad no existe. |
| 23 | **(fuera de los 22 del plan)** Ejecución real de tests | **COMPLETE** | `execute.test` (§5.4): pytest real dentro del sandbox, argv fijo, sin shell, con timeout, exit code, stdout, stderr y conteos verificados por tests con proyectos temporales reales. Registra en el catálogo, pasa por Policy y Gate, y produce Observation estructurada. |
| 22 | End-to-End | PARTIAL | Ciclo ejecutado con tools reales sobre un repo con bug real. Trayectorias distintas según contexto (5 decisiones sin replan vs 8 con 2 replans). **No demuestra cerrar un objetivo real**: el test del fixture sigue fallando y la misión se reporta completada. |

### Resumen

| Veredicto | Cantidad | Requisitos |
|---|---|---|
| COMPLETE | 8 | 5, 8, 9, 10, 11, 14, 15, 20 |
| PARTIAL | 11 | 1, 2, 3, 4, 7, 12, 13, 18, 19, 21, 22 |
| BROKEN | 1 | 11 |
| MISSING | 3 | 6, 16, 17 |

---

## 3. El patrón real: ALEXIS hace el ciclo pero no lo cierra

El requisito 22 es correcto y el resto se explica con una sola observación:

```
ENTENDER → RAZONAR → DECIDIR → ACTUAR → OBSERVAR → EVALUAR  = sí, implementado
REPLANIFICAR → VERIFICAR → APRENDER → RECORDAR                = no, o fingido
```

Lo que existe es un motor de **ejecución de pasos con reales-presupuesto de estado**.
Lo que falta es el tramo que convierte "ejecuté pasos" en "conseguí el objetivo, con
prueba". Ese es el GAP de mayor impacto.

---

## 4. GAP de mayor impacto: #11 Success Criteria (BROKEN)

**Por qué es el mayor:** todo el resto del ciclo hereda su honestidad de aquí. Sin
verificación a nivel de objetivo, el sistema necesariamente confunde acción exitosa
con objetivo conseguido, y todo lo demás (respuesta, reflection, evidence grade)
miente por construcción. Es el único requisito **BROKEN** — ya hay código que afirma
cumplirlo y no lo cumple.

**Causa raíz localizada (1 línea de código):**

```python
# alexis/autonomy/mission.py:8
def create(self, objective: str, envelope: MissionEnvelope) -> Mission:
    return Mission(id=..., goal=Goal(objective=objective), envelope=envelope)
    #                                        ^^^^ se pierde success_criteria
```

`success_criteria` se produce en el clasificador (`intent_classifier.py:233`), viaja
en el Intent, y se descarta al crear la misión (4 call sites en `apps/demo/server.py`).
Ningún otro punto del sistema lo lee: `grep` de `success_criteria` en `alexis/` solo
encuentra el contrato y su serialización.

**Agravante:** el catálogo ya declara `execute.test` y `terminal.run`
(`alexis/capabilities/catalog.py`), pero **ninguno tiene adaptador en el executor**
(`alexis/execution.py` solo despacha `fs.*` y `tts.speak`). Sin eso, verificar un
objetivo tipo "los tests pasan" es imposible con herramientas reales — y el plan prohíbe
resolverlo con mocks.

---

## 5. Plan exacto para cerrarlo

Incrementos pequeños, cada uno con tests y parada para reportar. Ninguno avanza a P1.

### 5.1 Persistir los criterios (cierra la causa raíz) — **COMPLETADO 2026-09-25**
- `MissionEngine.create(objective, envelope, success_criteria=None)` — parámetro
  opcional, retrocompatible, sin tocar los call sites existentes más que pasando el valor.
- `Goal.success_criteria` se puebla desde el Intent en el path de producción
  (`apps/demo/server.py:_create_mission_from_intent`).
- La lista se copia al Goal (sin aliasing al Intent) y la carga de una fila vieja sin la
  clave ya no rompe (`alexis/storage/serialization.py`).
- Test: `tests/test_success_criteria_persistence.py` (8 tests) — cadena real
  Intent→Mission→Goal, no-pérdida, orden, retrocompatibilidad, aliasing, round-trip de
  persistencia, fila vieja, y guarda de cableado del call site de producción.

### 5.2 Taxonomía de evaluación de 5 verdicts (cierra #10) — **COMPLETADO 2026-09-25**
- `Verdict` con los 5 valores y su contrato (qué significa y qué **no** significa).
- Los 9 caminos de retorno de `step()` emiten veredicto explícito; `_settle` es el punto
  único donde queda registrado en el `KnowledgeState` y por tanto persistido.
- `verdict_for_execution()` decide a partir de lo observado: una tool que termina sin error
  en silencio es `PARTIAL_SUCCESS`, no `SUCCESS`.
- Sin cambiar ninguna decisión que los tests anteriores ya fijaban.
- Tests: `tests/test_verdict_taxonomy.py` (29).

### 5.3 `GoalVerifier` con evidencia por criterio (cierra #15)
- Un criterio → un checker. Dos families iniciales: existencia/contenido de archivo
  (ya cubierto por `FilesystemVerifier`) y resultado de suite de tests.
- Devuelve por criterio: `satisfied`, `grade` (EVIDENCE / INFERENCE), `evidence_ids`.
- `INSUFFICIENT_EVIDENCE` cuando el criterio no tiene checker: nunca "satisfied" por defecto.

### 5.4 Ejecutar tests con la capability que ya existe en catálogo
- `execute.test` pasa de entrada sin adaptador a tool real: argv **fijo y allowlisted**
  (nunca shell), dentro del sandbox, sin red, con timeout y límite de salida.
- Riesgo `MEDIUM` → pasa por Policy y Gate; pide aprobación si el policy lo exige.
- No es una capability nueva: es activar una entrada que el catálogo ya declara.

### 5.5 Corregir la condición de éxito de la misión (cierra #11 y #22)
- `completed` exige `GoalVerifier` con todos los criterios `satisfied` por evidencia.
- Sin evidencia suficiente → `waiting_clarification` o `failed`, nunca `completed`.
- Regresión: el fixture del MVP debe dejar de reportar éxito.

### 5.6 Cerrar el ciclo (cierra #16, #17, #18)
- `ResponseComposer`: la respuesta se compone de acciones, observaciones, evidencia,
  verificación, incertidumbre y estado final. Prohibido "listo" sin verificación.
- `Reflector`: `outcome → reflection → lesson → ExperienceLearner`.
- `_record_decision` se invoca también en el camino cognitivo, para que `decisions`
  se persista y la misión sea auditable de principio a fin.

### 5.7 Prohibición de reintentos idénticos (cierra #12)
- Firma de acción = (capability, args canonizados). Si se repite sin progreso → no es
  replan: es bloqueo. Obliga a cambiar de estrategia, no a cambiar el id del paso.

### 5.8 Selección dinámica real (cierra #6)
- `CapabilitySelector` sobre el catálogo y el WorldModel: la capability se elige a
  partir del objetivo y de lo observado, no de una plantilla. Es el último incremento
  porque requiere 5.1–5.7 operativos para tener algo que elegir honestamente.

### 5.9 Cierre de P0
- Test de comportamiento `false success` a nivel de objetivo (hoy inexistente).
- Test de recuperación del loop cognitivo a mitad de misión.
- E2E: el fixture con el bug real termina en objetivo verificado o en fallo honesto.
- Re-auditar los 22 requisitos. P0 = 100% solo con los 22 COMPLETE.

---

## 6. Lo que este documento NO es

- No es un plan de P1. P1 sigue bloqueada.
- No declara P0 cerca de terminar. Falta el tramo que cierra el ciclo.
- No propone mocks para simular verificación. La opción de `execute.test` es real o
  el requisito 11 y 15 quedan abiertos.
- No reescribe lo que funciona. Los 5 requisitos COMPLETE y los 13 PARTIAL se
  extienden; el motor de decisiones existente es la base, no el objetivo a reemplazar.

---

## 7. Registro de cambio: §5.1 implementado

### Archivos modificados
| Archivo | Cambio |
|---|---|
| `alexis/autonomy/mission.py` | `create()` acepta `success_criteria` opcional y lo persiste en el `Goal` (copia defensiva). |
| `apps/demo/server.py` | `_create_mission_from_intent` pasa `success_criteria=intent.success_criteria`. |
| `alexis/storage/serialization.py` | `mission_from_row` lee la clave con `.get(...) or []`: una fila anterior a §5.1 ya no rompe la carga. |
| `tests/test_success_criteria_persistence.py` | Nuevo, 8 tests. |
| `docs/P0-COGNITIVE-CORE-GAP-ANALYSIS.md` | Este registro. |

### Requisito 11: BROKEN → PARTIAL
La causa raíz del audit está cerrada y probada. El requisito **no** pasa a COMPLETE:
persistir los criterios no es verificarlos. Siguen abiertos §5.3 (verificador con evidencia
por criterio) y §5.5 (`completed` exige verificación), y con ellos el defecto de fondo que
§4 describía.

### Hallazgo nuevo, fuera del alcance de §5.1
El **clasificador por reglas no produce `success_criteria`**: para todo utterance devuelve
`[]`. Los criterios solo existen en el path con modelo (`intent_classifier.py:233-245`,
verificado en runtime). Consecuencia: con el Model Router en DEGRADED o sin modelo — el
estado real del demo hoy, con `llama3.2:1b` — el `Goal` queda legítimamente vacío aunque la
persistencia ya sea correcta. No se ha tocado el clasificador: decidir si el fallback por
reglas debe derivar criterios del objetivo es un cambio de comportamiento que corresponde a
otro incremento, no a §5.1. Queda registrado como decisión pendiente del usuario.

### Call sites de creación de misión que siguen sin criterios
`POST /missions` (`apps/demo/server.py:836`) y `apps/api/main.py:93`) reciben JSON directo,
no un Intent, por lo que no tienen criterios que propagar. Añadir un campo
`success_criteria` a esas APIs públicas amplía su superficie y queda fuera de §5.1.

### Verificación
- `tests/test_success_criteria_persistence.py`: 8 passed.
- Suite completa: **438 passed**, 4 failed (los 4 ambientales preexistentes: fastapi ausente
  y claves de ElevenLabs montadas en el entorno de test). Ninguno relacionado con §5.1.
- Pyflakes limpio. Demo reiniciado y respondiendo HTTP 200 en el puerto 8100.

### Estado
P0 = **54.5%**. §5.2–§5.9 sin empezar. P0 no está al 100% y P1 sigue bloqueada.

---

## 8. GAPs pendientes registrados

GAPs que no se resuelven dentro del incremento en curso. Cada uno tiene que decidir su
propio incremento; mezclarlos aquí sería exactamente el error de alcance que el plan
prohíbe.

### GAP-P1 — El fallback por reglas no produce `success_criteria`

- **Estado**: abierto. No corregido a propósito.
- **Origen**: descubierto al implementar §5.1.
- **Comportamiento real**: `RuleBasedIntentClassifier` devuelve `success_criteria=[]` para
  todo utterance. Los criterios solo los produce el path con modelo
  (`alexis/cognition/intent_classifier.py:233-245`), y únicamente si el modelo los
  devuelve.
- **Condición en la que aparece**: siempre que el Model Router esté en `DEGRADED` o no
  haya modelo disponible. Ese es el estado real del demo hoy (`llama3.2:1b`).
- **Por qué importa**: §5.1 garantiza que los criterios lleguen al `Goal` y sobrevivan,
  pero si el clasificador no los produce, el `Goal` queda legítimamente vacío. ALEXIS no
  puede determinar criteria de éxito cuando su modelo principal está degradado, y el
  ciclo no tendrá contra qué verificar (§5.3, §5.5). Afecta a los requisitos 1, 7, 11, 15
  y 22.
- **Lo que NO se hizo**: no se parcheó el clasificador dentro de §5.1 ni se cambió su
  comportamiento. Que el fallback por reglas derive criterios del objetivo —y con qué
  grado de confianza, siendo un criterio una afirmación y no un hecho— es una decisión de
  diseño que pertenece a su propio incremento.
- **Criterio de cierre**: con el router en `DEGRADED`, una tarea llega al `Goal` con al
  menos un criterio, o el sistema dice explícitamente que no sabe derivarlos.

### GAP-P2 — Dos APIs de creación de misión no propagan criterios

- **Estado**: abierto.
- **Origen**: descubierto al implementar §5.1.
- `POST /missions` (`apps/demo/server.py:836`) y `apps/api/main.py:93` reciben JSON
  directo, no un Intent, así que no tienen criterios que propagar: el `Goal` nace vacío
  aunque la persistencia sea correcta.
- **Por qué no se corrigió**: añadir un campo `success_criteria` a esas APIs públicas
  amplía su superficie y excede §5.1.
- **Criterio de cierre**: decisión explícita del usuario sobre si esas APIs aceptan
  criterios, con test de contrato.
---

## 9. Registro de cambio: §5.2 implementado

### Archivos modificados
| Archivo | Cambio |
|---|---|
| `alexis/cognition/state.py` | Enum `Verdict` con contrato documentado + `is_conclusive`; `KnowledgeState` gana `last_verdict` y `verdict_reason` (con `to_dict`/`from_dict`); exportado en `__all__`. |
| `alexis/cognition/loop.py` | `StepOutcome` gana `verdict` (por defecto `INSUFFICIENT_EVIDENCE`) y `verdict_reason`; los 9 caminos de retorno de `step()` emiten veredicto; `_settle` los asienta en el `KnowledgeState`; helpers puros `verdict_for_execution()` y `_has_observation()`. |
| `tests/test_verdict_taxonomy.py` | Nuevo, 29 tests. |
| `docs/P0-COGNITIVE-CORE-GAP-ANALYSIS.md` | Este registro. |

### Requisito 10: PARTIAL → COMPLETE
Los cinco verdicts existen, tienen contrato y **se emiten**: no es una enumeración muerta.
Cada uno tiene al menos un test que lo produce en el bucle real.

### Mapa de veredictos (contrato aplicado)
| Camino de `step()` | Verdict | Razón |
|---|---|---|
| tool con observación | `SUCCESS` | devolvió algo que se puede mirar |
| tool sin error y sin salida | `PARTIAL_SUCCESS` | se ejecutó; no consta que sirviera |
| tool con error | `FAILURE` | se intentó y no consiguió |
| `verify` pasada / fallida | `SUCCESS` / `FAILURE` | de la **verificación del plan**, no del objetivo |
| plan inválido, policy deniega, esperando aprobación | `BLOCKED` | la autoridad o la validez lo impidieron |
| `ask_user` | `INSUFFICIENT_EVIDENCE` | falta información crítica |
| `replan` con fallo previo / sin fallo | `FAILURE` / `INSUFFICIENT_EVIDENCE` | no se inventa un fallo donde no lo hubo |
| `abort` por falta de avance | `FAILURE` | nunca un SUCCESS de consolación |
| `finish` | `INSUFFICIENT_EVIDENCE` | **terminar no es evidencia** |

### Lo que 5.2 deliberadamente NO hace
No declara ningún objetivo verificado. `finish` emite `INSUFFICIENT_EVIDENCE` aunque la
misión quede en `COMPLETED`: esa contradicción es la deuda de §4, ahora **visible** en
lugar de oculta, y la cierra §5.3 + §5.5. Hay dos tests que lo impiden explícitamente
(`test_finalizar_no_declara_el_objetivo_verificado`, `test_ningun_camino_terminado_emite_success`).
El requisito 11 sigue PARTIAL y el 15 también: el veredicto califica la acción, no el objetivo.

### Efecto colateral aceptado y documentado
`mission_state=COMPLETED` con `verdict=INSUFFICIENT_EVIDENCE` es incoherente a propósito.
El estado de misión sigue mintiendo hasta §5.5; el veredicto ya no. Prefiero la
incoherencia visible a la coherencia falsa.

### Verificación
- `tests/test_verdict_taxonomy.py`: 29 passed, 0 skipped.
- §5.1 intacto: `tests/test_success_criteria_persistence.py` 8 passed.
- Suite completa: **467 passed**, 4 failed (los 4 ambientales de siempre: fastapi ausente y
  claves de ElevenLabs montadas en el entorno de test). Ninguno relacionado con §5.2.
- Pyflakes limpio.

### Estado
P0 = **57.3%**. §5.3–§5.9 sin empezar. P0 no está al 100% y P1 sigue bloqueada.

---

## 10. Registro de cambio: §5.3 implementado

### Archivos
| Archivo | Cambio |
|---|---|
| `alexis/cognition/goal_verification.py` | Nuevo: `CriterionStatus`, `CriterionEvidence`, `CriterionEvaluation`, `GoalVerification`, `GoalVerifier`, `parse_predicate`. |
| `alexis/cognition/loop.py` | `CognitiveRuntime` acepta `goal_verifier` y expone `verify_goal()`, que registra el resultado en `mission.context["goal_verification"]`. Sin verificador inyectado no cambia nada. |
| `tests/test_goal_verifier.py` | Nuevo, 23 tests. |
| `docs/P0-COGNITIVE-CORE-GAP-ANALYSIS.md` | Este registro. |

### La separación, hecho estructural y no declarativo
`GoalVerifier.verify()` recibe la misión y las fuentes de evidencia. **No admite un
`ExecutionResult` ni un paso**: no existe forma de pasarle "la tool terminó bien" como si
eso probara el objetivo. Hay un test que fija la firma para que nadie la amplíe por
casualidad (`test_la_verificacion_no_recibe_el_resultado_de_la_tool`).

```
ACTION → OBSERVATION → VERDICT (§5.2) → GOAL VERIFICATION (§5.3) → FINAL STATE (§5.5)
```

### Criterio → evidencia → veredicto
Vocabulario deliberadamente estrecho: `file_exists:<ruta>`, `file_missing:<ruta>`,
`file_size_at_least:<ruta>:<n>`, que es exactamente lo que una tool de filesystem puede
observar de forma estructurada. Un criterio en lenguaje natural sin uno de esos
predicados queda en `insufficient_evidence` con el motivo escrito. No hay fuzzy matching
para deciding veredictos: el matching laxo entre términos solo sirve para **adjuntar** un
claim al criterio como constancia, nunca para darlo por cumplido.

`verified=True` exige las tres cosas: al menos un criterio, todos `satisfied`, y al menos
una evidencia de confianza por criterio.

### Tres prohibiciones, con test
- **`tool_success == goal_success`**: `test_una_tool_exitosa_que_no_prueba_el_objetivo`
  lee un archivo real con éxito y comprueba que el objetivo sigue sin verificar.
- **`model_claim == verified`**: `test_un_claim_del_modelo_no_satisface_un_criterio` y
  `test_un_fact_sonante_tampoco_sustituye_a_la_observacion`. Un claim se registra como
  evidencia no fiable y no influence el veredicto.
- **verified sin evidencia suficiente**: `test_un_satisfied_sin_evidencia_fiable_no_verifica`
  exercise la guarda directamente, y `test_sin_criterios_no_se_puede_declarar_verificado`
  cubre el caso de GAP-P1 (objetivo sin criterios).

### Lo que 5.3 NO hace (deliberadamente)
- **No toca el estado final de la misión.** `verify_goal()` mide y registra; que
  `completed` exija objetivo verificado es §5.5. Hay un test que lo fija:
  `test_el_runtime_registra_la_verificacion_sin_tocar_el_estado` comprueba que la misión
  sigue en `pending` tras verificarse.
- **No implementa `execute.test`** (§5.4). Por eso "los tests pasan" sigue sin checker y
  queda en `insufficient_evidence`: es el estado honesto, no un olvido.
- No toca `Policy`, `Gates`, `Envelope` ni `FilesystemVerifier`.

### Verificación
- `tests/test_goal_verifier.py`: 23 passed. Toda la evidencia que sostiene un `satisfied`
  procede de `build_filesystem_tools` sobre el `tmp_path` del test: no hay mocks de
  evidencia.
- §5.1 y §5.2 intactos: 8 + 29 = 37 passed.
- Suite completa: **490 passed**, 4 failed (las 4 ambientales de siempre).
- Pyflakes limpio.

### Dos bugs reales encontrados al implementar
1. `parse_predicate` se comía el resto de la frase como argumento del predicado
   (`"notas.txt está escrito"`), así que ningún lookup del WorldModel encontraba la ruta y
   todo quedaba en `insufficient_evidence`. Ahora toma solo el primer token.
2. `_claims_about` leía `evidence.items`, pero `EvidenceStore` guarda en `evidence.claims`:
   los claims del modelo no se adjuntaban nunca y las evaluaciones perdían trazabilidad.

### Estado
P0 = **59.1%**. §5.4–§5.9 sin empezar. P0 no está al 100% y P1 sigue bloqueada.

---

## 11. Registro de cambio: §5.4 implementado (`execute.test`)

### Archivos
| Archivo | Cambio |
|---|---|
| `alexis/tools/testrunner.py` | Nuevo: `TestRunnerTool`, `build_test_tools`, validación de argumentos, `parse_pytest_counts`. |
| `alexis/execution.py` | `_CAPABILITY_TOOL` gana `"execute.test": "execute.test"`. Sin esto la capability no era alcanzable por la vía normal. |
| `alexis/world/model.py` | Kind `TEST` y registro de la suite observada en `observe_execution` (también cuando falla). |
| `alexis/cognition/goal_verification.py` | Dos predicados nuevos: `tests_passing` y `tests_failing`. Aditivo; ningún test de §5.3 cambia. |
| `apps/demo/server.py` | Registra `execute.test` en el `ToolRegistry`. |
| `tests/test_execute_test_capability.py` | Nuevo, 50 tests. |
| `docs/P0-COGNITIVE-CORE-GAP-ANALYSIS.md` | Este registro. |

### Mecanismo de sandbox: el que ya existía
Se reutiliza `SandboxRunner`, sin segundo mecanismo de aislamiento: cwd acotado al
workspace (`resolve_in_workspace` bloquea traversal y rutas absolutas fuera), env limpio y
fijo (`PATH`, `HOME`, `LANG`, `PYTHONDONTWRITEBYTECODE`; nada de credenciales), `RLIMIT`
de CPU/AS/FSIZE/NOFILE/NPROC vía `preexec_fn`, timeout con kill y tope de salida de 64 KB.

### Controles de seguridad
- **argv fijo, nunca shell**: `create_subprocess_exec` con lista. Sin `shell=True`, sin
  `bash -c`, sin concatenación. La inyección no se previene: es imposible por la vía.
- **Argumentos en lista blanca**: solo `path`, `runner`, `selectors`, `timeout`.
  `command`, `cmd`, `shell`, `script`, `args`, `extra_args` y `env` se rechazan con nombre
  explícito; cualquier clave desconocida también.
- **`runner` limitado a `pytest`**: añadir otro es decisión de seguridad, no argumento.
- **Selectores validados**: patrón estricto de node id + rechazo de metacaracteres de
  shell. Los corchetes se permiten a propósito (ids parametrizados de pytest).
- **`path` dentro del workspace** y debe existir y ser directorio.
- **`timeout` acotado** a [1, 300] s; por defecto 120 s.
- **Riesgo declarado `medium`**, no `low`: ejecutar una suite ejecuta código de terceros.
  La aprobación la resuelve Policy según el riesgo del paso, sin tocar las reglas.
- **No se toca** `Policy`, `AutonomyGate` ni `MissionEnvelope`.

### Evidencia generada
`ok=True` significa **la suite se ejecutó y terminó con exit code 0**. `ok=False` con
`tests_failed > 0` significa que la suite corrió y falló: la acción se hizo y el resultado
es el que es. En ambos casos viajan `command` (argv), `cwd`, `exit_code`, `stdout`,
`stderr`, `duration_ms`, `timed_out`, `tests_passed`, `tests_failed`, `tests_skipped`,
`counts_parsed` y `selectors`. El `ExecutionResult` lleva su `Observation` con
`trusted=True`, y el WorldModel la registra como entidad `TEST` con
`source=tool:execute.test`, que es lo que permite al GoalVerifier evaluar un criterio de
tests con evidencia fiable.

`counts_parsed=False` cuando el resumen de pytest no se pudo leer: nunca se inventa un 0.

### La separación sigue en pie
`execute.test` exitoso **no** verifica el objetivo. Hay dos tests que lo fijan:
`test_suite_que_pasa_no_verifica_el_objetivo_sin_observerla` (tool ok, objetivo sin
verificar) y la cadena de §5.3, que sigue siendo la autoridad.

### Verificación
- `tests/test_execute_test_capability.py`: **50 passed**, con proyectos temporales reales y
  pytest de verdad. El timeout se provoca con un test que duerme 30 s; el fallo, con un
  test que falla de verdad. Cero mocks del resultado de una suite.
- §5.1, §5.2, §5.3 intactos: 8 + 29 + 23 = 60 passed.
- Suite completa: **540 passed**, 4 failed (las 4 ambientales de siempre).
- Pyflakes limpio.

### GAPs nuevos
- **GAP-P3 — la red no está bloqueada, solo desatendida.** `SandboxRunner` no inyecta red ni
  credenciales, pero no la bloquea: eso exige namespaces con privilegios y su propio
  docstring lo dice. `execute.test` hereda ese límite. Pasa a ser deuda de P8, no de P0.
- **GAP-P4 — un paso `test` sin `args` recibe el path legacy `README.txt`.** El default sin
  args del executor es específico de filesystem; para una suite no significa nada y la
  tool lo rechaza. Hoy el planner no produce pasos `test`, así que no muerde, pero cuando
  haya selección dinámica (§5.8) habrá que fijarlo.
- **GAP-P5 — `default_risk="low"` en el catálogo para una capability que ejecuta código.**
  El catálogo declara `execute.test` como riesgo bajo desde que era un stub. La tool
  declara `medium` y es lo que se ve, pero el catálogo miente y eso acabará importando en
  cuanto algo lea `default_risk`.
- **Nota**: `PytestCollectionWarning` por `TestRunnerTool` (pytest intenta colectarla como
  clase de test). Inofensivo, pero el nombre de la clase invites a renombrarla.

### Estado
P0 = **61.4%**. §5.5–§5.9 sin empezar. P0 no está al 100% y P1 sigue bloqueada.

---

## 12. Registro de cambio: §5.5 implementado (`COMPLETED` exige objetivo verificado)

### Caminos hacia `COMPLETED` encontrados (y cerrados)
| # | Camino | Antes | Ahora |
|---|---|---|---|
| A | `core/runtime.py` legacy `run_mission` | `COMPLETED if verification.passed`, con el verificador del **plan** | `settle(mission, self._verify_goal(mission))` |
| B | `core/runtime.py` rama `VERIFY` (cognitivo) | `if outcome.verification.passed: COMPLETED` | el plan verifica; `settle()` decide |
| C | `core/runtime.py` rama `FINISH` (cognitivo) | fabricaba un `Verification(passed=True)` para poder cerrar | `settle()`; sin objetivo verificado la misión sigue viva |
| D | `cognition/loop.py` rama `FINISH` | devolvía `mission_state=COMPLETED` | llama a `verify_goal()` y delega en `settle()` |
| E | `Mission(id, goal, envelope, state=COMPLETED)` | construía una misión completada | `UnverifiedGoalError` en el `__init__` |
| F | `storage/serialization.py` al recuperar | `MissionState(row["state"])` a ciegas | restaura la verificación y solo reconstruye `completed` si es válida; si no, degrada con motivo |
| G | `alexis/storage/__main__.py` | `mission.state = COMPLETED` en el smoke test | usa `settle()` con verificación real |
| H | API y demo | — | no ofrecen ninguna vía: lo comprueba un test que escanea el código de producción |

### La regla, estructural
`Mission.__setattr__` rechaza `COMPLETED` salvo que `goal_is_confirmed(goal_verification)`
sea cierto, y esa función **revalida** la verificación criterio por criterio. No se trusts
una bandera: un `GoalVerification(verified=True)` fabricado, o uno serializado con
`verified: true` y sin evidencia fiable, no abre la puerta.

`alexis/autonomy/goal_state.py` añade la política en un único sitio:
- `settle(mission, verification)` → `COMPLETED` / `NEEDS_VERIFICATION` / `BLOCKED`.
- `COMPLETED` solo con `verified=True` y todos los criterios `satisfied` con evidencia fiable.
- `NEEDS_VERIFICATION` cuando no hay evidencia suficiente: **no verificado no es fallido**.
- `BLOCKED` cuando hay al menos un criterio incumplido: hay un hecho en contra.
- La verificación se adjunta a la misión **y** a `mission.context`, para que la fila
  persistida pueda reconstruir un `completed` legítimo.

### Regresión anti-false-success
`test_regresion_accion_exitosa_nunca_por_si_sola_completa`: la acción funciona, el
verificador del plan pasa, y el objetivo sigue sin demostrarse → el sistema lo impide.
Comprobado de verdad: reintroduciendo `mission_state=COMPLETED` en la rama `FINISH` del
loop, **7 tests de §5.5 fallan**. El guardián de producción
(`test_ningun_modulo_de_produccion_asigna_completed_fuera_de_la_autoridad`) escanea
`alexis/` y `apps/` y falla si alguien vuelve a asignar `COMPLETED` fuera de la autoridad.

### Tests (24 nuevos, `tests/test_completed_requires_verified_goal.py`)
Los 12 casos obligatorios, con dos derivadas que importan: la **persistencia** (§F) y la
**recuperación** (§F) no eran un detalle, eran un agujero. Tests de regresión de
construcción, de verificación fabricada, de estado persistido y de escaneo de producción.

### Cambios en tests existentes (24 tests adaptedos)
Ninguno cambiaba su sujeto; lo que cambiaba era el estado que afirmaban. Donde el objeto
del test era "la puerta no bloquea el trabajo legítimo" (gate, self model, world model,
reutilización de plan) se afirma el estado honesto; donde el objeto era la finalización
(`s3_s4`, `autonomy_core`, `runtime_persistence`) se añadió `WorldModel` + `GoalVerifier` y
criterios que describen el objetivo real: leer exige `file_exists:reporte.txt`, borrar
exige `file_missing:para-borrar.txt`. La evidencia es de tools reales sobre archivos reales.

### GAPs descubiertos
- **GAP-P6 — `mission.results` no existe en el camino cognitivo.** El runtime legacy
  acumula `results`; el cognitivo solo tiene el `KnowledgeState`. Cualquier consumidor de
  `results` ve una lista vacía encognitive. No es un problema de §5.5, pero es una
  inconsistencia real entre los dos caminos.
- **GAP-P7 — el verificador de plan y el de objetivo pueden discrepar y nadie lo explica.**
  `run_mission` puede dejar la misión en `VERIFYING` con un criterio incumplido, y el
  siguiente paso vuelve a evaluarlo. Funciona, pero el estado de comprobación del plan
  se descarta: solo queda el del objetivo.
- **Nota de alcance**: `_finalize()` ya solo registra learning cuando el objetivo está
  verificado. No se ha añadido `Reflector` (eso es §5.6).

### Verificación
- `tests/test_completed_requires_verified_goal.py`: 24 passed.
- §5.1–§5.4 intactos: 8 + 29 + 23 + 50 = 110. Con §5.5: 134 passed.
- Suite completa: **564 passed**, 4 failed (las 4 ambientales de siempre).
- Pyflakes limpio. Sin cambios en `Policy`, `AutonomyGate` ni `MissionEnvelope`.

### Estado
P0 = **61.4% de los 22 requisitos del plan** (8 COMPLETE, 11 PARTIAL, 3 MISSING, 0 BROKEN).
§5.6–§5.9 sin empezar. P0 no está al 100% y P1 sigue bloqueada.

---

## 13. Registro de cambio: §12 Replanning cerrado (2026-09-26)

El requisito 12 pasa de **PARTIAL** a **COMPLETE**. Los 24 tests previos seguían en verde
y, aun así, el sistema repetía la lectura en el flujo real. Lo que lo destapó fue el test
de integración (§13.3), no la lógica de la guarda.

### 13.1 Qué se implementó

- **Firma determinista** de la acción `(action, capability, args normalizados)`, sin id de
  paso: `read-probe` y `read-probe-2` con los mismos argumentos son la MISMA acción.
- **Filtro en producción**, dentro de `options()`, que es el punto por el que pasan todas
  las decisiones: descarta la repetición si la acción es de un replan, su firma ya se
  intentó, aquel intento falló y no hay evidencia material nueva.
- **Procedencia** por generación (plan original = 0, replan ≥ 1) y por `context_version`.
- **Recuperación**: la firma, la procedencia y la huella sobreviven a la serialización, así
  que tras un reinicio la decisión es la misma.

### 13.2 Corrección de §12.5: qué es "evidencia material"

La regla original era demasiado laxa y el propio enunciado lo señalaba. La evidencia
material es la que **habla del objeto sobre el que actuó la acción** y existe como hecho
observado:

| Cuenta | No cuenta | Motivo |
|---|---|---|
| `WorldModel` sobre el target | `known` | mezcla datos con libro de cuentas («x» completado) |
| claims de origen externo | `completed_steps` | es el resultado, no la causa |
| | `unknown` | es ignorancia declarada |
| | `replans`, contadores | contadores del ciclo |

Se implementó el **evidence scope**: el objetivo se deriva del argumento `target`
(`path`/`file`/`target`/`source`/`dest`/`query`/`url`/`uri`/`name`). Sin scope identificable
se usa evidencia global, porque no se puede probar la relevancia pero tampoco se debe
bloquear la acción para siempre.

### 13.3 El fallo que encontró la integración

El autoparte del sistema. Al ejecutar, el Core registra dos claims propios —
`observation:tool.fs.read` y `executor`— cuyos textos **citan la ruta del objetivo**. Como
la huella los incluía, cambiaba en cada intento, el filtro leía «hay evidencia nueva» y
autorizaba la repetición siempre. Con el `WorldModel` vacío.

Los 24 tests unitarios pasaban porque ninguno reproducía la observación real del executor.
El ciclo completo por `run_mission` (`tests/test_p0_12_gaps.py::test_g2_06`) lo reproduce y
lo cazó: se ejecutaban `leer_1` **y** `leer_2`.

Corrección en dos puntos:
1. `_is_self_record()` excluye los claims que el sistema se hace a sí mismo al ejecutar la
   capability que se reintenta. El autoparte de **otra** capability (`fs.stat` observando
   que el archivo existe) sí cuenta, que es el caso bueno.
2. Se hashea el **contenido** del claim, no su `id`. Los ids son únicos por registro, así
   que un mismo hecho anotado dos veces parecía evidencia nueva.

### 13.4 Auditoría del rechazo

`replan.action_rejected` se emite por la infraestructura que ya existía —`EventBus` +
`AuditRepository`→`audit_log`—, sin logger paralelo. El reparto es el del epílogo de §5.6:
el Core decide y acumula (`drain_rejections()`), el runtime publica y persiste
(`_flush_rejections()`), porque `options()` es sync y los repos son async.

Se guarda además en `mission.context["replan_rejections"]`, que la Storage persiste, para
poder responder «¿por qué ALEXIS descartó esta acción?» tras un reinicio incluso sin BD.

**Invariante que se respeta:** el fallo de auditoría no tumba el bucle cognitivo. Se
registra como warning y el ciclo sigue. `audit_log.mission_id` tiene FK a `missions`, así
que auditar exige que la misión esté persistida; `run_mission` ya lo hace en su primer
`_commit`.

### 13.5 Verificación

- `tests/test_p0_12_gaps.py`: 23 passed (14 de evidencia, 5 de auditoría, 1 de integración,
  3 de regresión del autoparte).
- `tests/test_p0_12_replanning.py`: 24 passed.
- Suite completa: **741 passed**, 0 regresiones. `compileall` y `tsc --noEmit` limpios.
- `test_g2_05` fija que el filtro no toca `Policy`, envelope ni registry, y no crea
  `approved_step_ids`.
- Un test existente cambió de expectativa, con motivo documentado: `test_d3` simulaba
  «evidencia nueva» con `add_known(...)`, que es justo lo que la regla corregida excluye.
  Ahora usa un hecho observado, que es el ejemplo del propio enunciado.

### 13.6 Estado

Cognitive Core ≈ **86%**. COMPLETE 15 · PARTIAL 7 · MISSING 0 · BROKEN 0.
