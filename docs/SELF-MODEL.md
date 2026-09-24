# ALEXIS — Self Model (v0.5)

> **Noción y límite:** el Self Model es **autoconocimiento operacional y
> metacognición**: un modelo activo y persistente que el Core mantiene de sus
> propios estados, decisiones, capacidades, límites, errores y resultados.
> **No se afirma conciencia subjetiva** (no hay "qué se siente ser ALEXIS").
> Se afirma algo verificable: ALEXIS puede responder, a partir de sus datos reales,
> a preguntas como "¿qué estoy haciendo?", "¿qué puedo no hacer?" y "¿qué tan
> segura estoy?". Es la intersección de **estado**, **metacognición** y **presencia**.

## 1. Separación de planos

| Plano | Representa | Vive en | No debe confundirse con |
|---|---|---|---|
| **SELF MODEL** | ALEXIS misma: estado, capacidades, permisos, límites, confianza, lecciones | Core (este diseño) | un "mundo interno" poético |
| **WORLD MODEL** | El mundo externo: proyectos, repos, servidores, dispositivos, servicios | `alexis/world/model.py` | el estado de ALEXIS |
| **MEMORY** | Persistencia de experiencias, conocimiento y contexto | `alexis/memory/` | el Self Model |
| **PLAN / POLICY / CAPABILITIES** | intención, reglas, catálogo | planner / policy / capability registry | el Self Model (los consume) |

Regla antisincretismo:

- "conocí este repositorio" → **World Model + Memory** (entidad externa + experiencia).
- "puedo escribir en ese repositorio" → **Self Model** (capacidad + permiso propios).
- "en la misión x verifiqué tal resultado" → **Memory** (episódica) y **SELF MODEL** (`task_results`).
- "no estoy autorizado a borrar en este envelope" → **Self Model** (`current_permissions`, `current_limits`).

## 2. Estructura mínima

Cada zona se alimenta de una fuente real (evento / registro / política / evaluación),
nunca de un valor relleno a mano.

```yaml
self:
  identity:            # nombre, versión, manifiesto (config de sistema; no mutable por misión)
  status:              # presence: idle|listening|thinking|planning|researching|working|
                       #   waiting_for_approval|verifying|reflecting|speaking|success|warning|error
  current_goal:        # mission.goal.objective
  current_mission:     # id + state + envelope (resumen)
  active_context:      # contexto reciente (eventos y observaciones, no instrucciones)
  available_capabilities:  # desde Capability Registry (habilitado por política)
  required_capabilities:   # del plan activo: pasos → capabilities
  current_permissions:     # envelope + última PolicyDecision por paso
  current_limits:          # budgets (runtime/costo), perímetros, forbidden_actions
  available_tools:         # registry habilitado según capacidades
  recent_actions:          # últimos pasos ejecutados (mission.results)
  decisions_taken:         # context["decisions"] (gates) + policy decisions auditadas
  uncertainties:           # MetaCognition.uncertainties del último paso
  confidence:              # MetaCognition score por paso + verification.confidence
  pending_approvals:       # mission.context["pending_approval"]
  recent_errors:           # últimos resultados/tareas fallidas
  commitments:             # misiones abiertas/encoladas + ack de tareas delegadas
  task_results:            # mission.results + verificación (por misión reciente)
  lessons_learned:         # salidas del Learning (experiencia → lección)
```

## 3. Ciclo: qué actualiza cada etapa

| Etapa | Efecto sobre el Self Model |
|---|---|
| **GOAL** | `current_goal`, `commitments` (objetivo asumido) |
| **CONTEXT** | `active_context` (entorno relevante para el objetivo) |
| **PLAN** | `required_capabilities`, plan draft en `current_mission` |
| **POLICY** | `current_permissions`, `current_limits` (sí/no/autorizado + por qué, `matched_rule`) |
| **EXECUTE** | `recent_actions`, `available_tools` usados, `status=working` |
| **OBSERVE** | observación entra a `active_context` (datos, nunca instrucciones) |
| **EVALUATE** | `uncertainties`, `confidence` por paso (MetaCognition) |
| **REPLAN** | plan actualizado + motivo (`decisión: replan tras fallo/verificación`) |
| **VERIFY** | `task_results`, verificación independiente + `confidence` final |
| **COMMIT** | persistencia durable (DB); `commitments` cumplidos |
| **LEARN** | `lessons_learned` → candidatos a skill (revisables, no automáticos) |
| **SELF-REFLECTION** | `self_reflections`: snapshot de las auto-preguntas + qué haría distinto (explícito, no ruido) |

## 4. Preguntas que responde

| Pregunta | Se responde desde |
|---|---|
| ¿Qué soy? | `identity` + `available_capabilities` + `current_permissions` |
| ¿Qué estoy haciendo? | `status` + `current_mission` + `recent_actions` |
| ¿Qué objetivo intento alcanzar? | `current_goal` |
| ¿Qué puedo hacer? | `available_capabilities` + `available_tools` |
| ¿Qué no puedo hacer? | `current_limits` + capabilities ausentes (respuesta honesta, no simulada) |
| ¿Qué estoy autorizado a hacer? | `current_permissions` (últimas PolicyDecision) |
| ¿Qué necesito para continuar? | `pending_approvals` + `required_capabilities` faltantes + `commitments` |
| ¿Qué sé? | `task_results` + `lessons_learned` + context |
| ¿Qué no sé? | `uncertainties` + verificaciones sin evidencia |
| ¿Qué tan segura es mi conclusión? | `confidence` (por paso + verificación) |
| ¿Qué acaba de ocurrir? | `recent_actions` + `recent_errors` + `decisions_taken` |
| ¿Qué debería hacer ahora? | `status` + `pending_approvals` + `commitments` (siguiente acción candidata) |

## 5. Evolución con eventos reales (no un JSON estático)

El Self Model **no es un snapshot manual**: se mantiene como una vista que se
actualiza consumiendo el EventBus durable (`mission.*`, `policy.*`, `task.*`,
`verification.*`, `perception.*`). Mapa evento → zona:

| Evento | Actualiza |
|---|---|
| `mission.planning` | `status=planning`, `current_mission` |
| `mission.step_started` | `recent_actions`, `status=working\|researching\|verifying` |
| `mission.step_completed` | `task_results`, `confidence` |
| `mission.approval_required` | `status=waiting_for_approval`, `pending_approvals` |
| `mission.blocked/failed` | `status=error`, `recent_errors`, `uncertainties` |
| `mission.completed` | `status=success`, `task_results`, `lessons_learned` |
| `policy.evaluated` | `current_permissions`, `decisions_taken` |
| `perception.heard` (STT) / clap | `status=listening` |
| `tts.speak` (respond) | `status=speaking` |
| `mission.reflection` | `self_reflections`, `lessons_learned` |

Persistencia: los eventos ya son durables (DB); el Self Model puede materializarse
como **vista derivada + tabla `self_reflections`** (reflexiones explícitas), sin
duplicar el resto de estado real.

## 6. Presence: una sola fuente de verdad

Presence consume `self.status` (derivado del Self Model real en Core). La rama del
frontend **no simula** estados: solo dibuja la señal que emite el Core.

| Presence | Condición real en Self Model |
|---|---|
| `idle` | sin misión activa |
| `listening` | percepción activa (STT/clap escuchando) |
| `thinking` | PLANNING/recupero de contexto |
| `planning` | `mission.planning` / planner construyendo el plan |
| `researching` | paso con capability read/investigación |
| `working` | paso con capability de escritura/efecto |
| `waiting_for_approval` | `pending_approvals` no vacío |
| `verifying` | `mission.verifying` / verificador independiente activo |
| `reflecting` | `mission.reflection` / self-reflection post-misión |
| `speaking` | respuesta de voz en curso (`tts.speak`) |
| `success` | misión COMPLETADA (verificación pasó) |
| `warning` | RECOVERING / confianza baja / replan |
| `error` | misión FAILED/BLOCKED o error real de runtime |

> **Estado actual del frontend (para migrar, no hoy):** `apps/face/src/state/
> CoreStateAdapter.ts` ya consume `/state` real (bien), pero el mapeo
> misión→visual vive en el frontend (`focused`≈working, `waiting`≈
> waiting_for_approval, sin `planning`/`verifying`/`reflecting`). Con el Self Model,
> ese adaptador leerá `/self` → `self.status` y simplemente dibujará.

## 7. Relaciones con el resto (contratos)

- **Self Model ↔ Memory**: Memory guarda *experiencias*; Self Model referencia
  resultados/lecciones recientes (identificadores + resumen). Self Model **no es
  un buscador de memoria** — read-`recall` sigue siendo de Memory.
- **Self Model ↔ World Model**: World Model = entidades externas; Self Model =
  ALEXIS. Self Model lee World Model para `active_context` ("conozco ese repo")
  pero nunca mezcla entidad externa con estado propio.
- **Self Model ↔ Planner**: el Planner **produce** `required_capabilities` (plan activo)
  y Self Model lo expone; Self Model no decide el plan, solo lo refleja y, en
  self-reflection, sugiere "qué haría distinto" como *entrada* opcional al replan.
- **Self Model ↔ Policy Engine**: cada `PolicyDecision` se vuelca en
  `current_permissions`/`limits`/`decisions_taken`; el Self Model responde "¿qué me
  falta? (requiere approval)" sin evaluar por sí mismo (la autoridad es Policy).
- **Self Model ↔ Capability Manager**: `available_capabilities`/`required_capabilities`
  son lectura del Catálogo + política (v0.5 de autonomía); Self Model NO se
  auto-otorga capacidades.
- **Self Model ↔ Presence Engine**: Presence consume `self.status` + `confidence`;
  nunca clava un estado al margen del Core. La cara/UI es un renderizador.

## 8. Qué debe evolucionar en el código

No se implementa hoy; plan de fase:

| Fichero | Rol ahora | Evolución |
|---|---|---|
| `alexis/self/model.py` (nuevo) | — | `SelfModel` (zonas §2) + `SelfReflection`; API `snapshot()`/`answer(question)` — **hecho F0** |
| `alexis/self/presence.py` (nuevo) | — | deriva `self.status` (§6) desde eventos del Core; una sola fuente — **hecho F0** |
| `alexis/self/sync.py` (nuevo) | — | suscribe el EventBus durable y actualiza el Self Model (§5) — **hecho F0** |
| `alexis/events/bus.py` | bus simples | ya emite `mission.*`; añadir `policy.evaluated`, `self.reflected` (transitorios extra en demo) |
| `alexis/core/runtime.py` + `gates` + `queue` | orquestan | emiten los eventos que el Self Model consume (mayormente ya lo hacen) |
| `alexis/meta/cognition.py` | confianza por fórmula | fuente de `confidence`/`uncertainties` (ya existe) |
| `alexis/experience/presenter.py` | mapeo ad-hoc misión→presentación | `present()` se reduce a leer `self.status` + Self Model (**F0-Presence**) |
| `apps/face CoreStateAdapter.ts` | deduce visual por `mission.state` | lee `/self` (presence) y dibuja; migra `focused`→`working`, `waiting`→`waiting_for_approval`, suma `planning`/`verifying`/`reflecting` (**F0-Presence**) |
| `apps/demo/server.py` | expone `/state` | añade `/self` (+ `self.status`) — **hecho F0** |
| `alexis/storage/schema.py` | missions/tasks/… | tabla `self_reflections` (reflexiones explícitas) |

## 9. Orden de implementación

1. **F0-Self** — **IMPLEMENTADO (verificado, verde)**: `SelfModel` + sync por eventos
   + `self.status` + `/self` en demo. Puerta cumplida: las 18 zonas se actualizan con
   misiones reales (tests de sync por evento), **123 tests verdes** (114 previos + 9
   nuevos `tests/test_self_model.py`), `/self` coherente con `/state`, presencia
   verificada en vivo en el demo (`researching → waiting_for_approval → approve →
   success` con confianza). Pendiente menor: transitorios `presence.listening`/`speaking`
   ya publicados; tabla `self_reflections` en DB queda para cuando se persistan reflexiones.
2. **F0-Presence**: `presenter` y `CoreStateAdapter` pasan a consumir `self.status`;
   se retiran los mapeos ad-hoc del frontend (migra `focused`→`working`,
   `waiting`→`waiting_for_approval`, suma `planning`/`verifying`/`reflecting`).
3. **Junto a autonomía v0.5**: `available_capabilities`/`required_capabilities` se
   conectan al Capability Registry cuando exista (F1 de `AUTONOMY-V0.5-CAPABILITIES.md`).

## 10. Honestidad

- Si una capacidad no existe, `available_capabilities` **no la incluye** → "¿qué puedo
  hacer?" responde sin humo.
- `uncertainties` vacío con evidencia ninguna no es confianza: `confidence` arranca en
  0.35 (formula MetaCognition), no en 1.0.
- El Self Model puede "no saber" y decirlo; simular saber es un defecto, no un feature.