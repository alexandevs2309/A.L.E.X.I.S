# ALEXIS — Autonomía por Capacidades (v0.5)

> Dado: ALEXIS deja de ser un sistema **permanentemente restringido** y pasa a ser un
> sistema **autónomo con capacidades gobernadas por políticas**.
> La seguridad permanece: cambia la forma, no la obligación. En vez de restricciones
> globales (un workspace, sin red, sin herramientas externas, borrar siempre con
> aprobación), ALEXIS tendrá un modelo de cinco planos evaluados por contexto.

## 1. Principio rector

Un solo workspace, sin red, sin herramientas externas o el flujo fijo
`understand → research → execute → verify` fueron correctos para el **primer vertical
slice de seguridad (M1)**. No son la arquitectura definitiva.

A partir de aquí:

```
CAPABILITY  = lo que ALEXIS puede hacer (catálogo, definido por el sistema)
POLICY      = bajo qué condiciones puede hacerlo (reglas, configurables)
APPROVAL    = cuándo necesita autorización humana (riesgo + contexto + nivel)
SANDBOX     = dónde puede ejecutarlo de forma aislada (perfil por capacidad)
ENVELOPE    = alcance concreto de una tarea (declarado por el usuario)
```

Quiénes deciden cada plano:

| Plano | Decide | Pregunta que responde |
|---|---|---|
| CAPABILITY | El sistema (registro de capacidades) | ¿Puede hacer esto? |
| POLICY | El operador/admin (reglas de política) | ¿Bajo qué condiciones? |
| APPROVAL | El humano (o delegación explícita) | ¿Cuándo hay que preguntar? |
| SANDBOX | El runtime (perfil técnico de ejecución) | ¿Dónde, aislado? |
| ENVELOPE | El usuario al crear la misión | ¿Cuál es el alcance de *esta* tarea? |

Orden de evaluación en runtime (por paso):

`capability habilitada → policy(capability, recurso, perímetro, contexto) → approval
si procede → sandbox(perfil de capacidad) → ejecución → observación → verificación
solo sobre lo que se autorizó`

## 2. Catálogo de capacidades

Cada capacidad tiene un metadato explícito (`CapabilitySpec`):

```yaml
capability:
  id: "git.commit"
  sphere: "project"           # corta, ran_rutan... ver §3
  network: false              # requiere red?
  side_effects: true          # muta algo?
  trust_domain: "git"         # credenciales/dominio
  sandbox_profile: "project-terminal"
  default_risk: HIGH
  audit: full
  plans_action: "modify"      # acción del plan que la invoca
  requires_input: "commit message"
```

Catálogo completo (estado honesto, enero-2026):

| Capacidad | Esfera | Estado actual |
|---|---|---|
| `fs.read` / `fs.stat` / `fs.write` / `fs.remove` | Filesystem | ✅ implementada (solo workspace) |
| Desktop tools (chrome/spotify/claude/binance/cursor) | Desktop/host | ✅ delegado host |
| `tts.speak` (voz saliente) | Voice | ✅ implementada |
| STT (voz entrante) | Voice | ⏳ requiere mic externo (mic interno roto) |
| Terminal (subprocess con whitelist) | Project/host | 🔸 `SandboxRunner` existe; sin whitelist aún |
| Git / GitHub | Project | ⛔ no implementado |
| Browser / Web research | Web | ⛔ sólo abrir app de escritorio |
| MCP | Integraciones | ⛔ |
| APIs externas | Integraciones | ⛔ |
| Codex / coding agents | Integraciones | ⛔ |
| OpenCode | Integraciones | ⛔ |
| Obsidian | Integraciones | ⛔ |
| OmniRoute | Integraciones | ⛔ |
| Hermes | Integraciones | ⛔ |
| Vision / screen | Perception | ⛔ |
| IoT / MQTT / Home Assistant | Physical | ⛔ |

`✅` implementada · `🔸` base existente · `⛔` no implementada (adaptador pendiente).

Regla de diseño: **una capacidad no se declara disponible hasta que tiene adaptador
real, sandbox de perfil y política**. ALEXIS nunca finge una capacidad que no tiene.

## 3. Esferas (perímetros) y sandbox

En lugar de "el único workspace", ALEXIS tiene **perímetros** nombrados que el
runtime crea por perfil:

| Perfil | Alcance | Red | Uso típico |
|---|---|---|---|
| `sandbox-project` | directorio del proyecto autorizado | ❌ | fs, tests, git local |
| `sandbox-terminal` | proyecto + comandos whitelist | ❌ | build/run determinista |
| `browser-sandbox` | navegador headless aislado | ✅ observada | investigación web |
| `host-delegated` | delegado del host (apps de escritorio, voz) | aparte | acción de escritorio |
| `network-observed` | proceso con egress monitorizado, sin credenciales | ✅ logueada | API externa delegada |

Invariante que **no** cambia: `least privilege`. El cambio es que el privilegio se
otorga **por capacidad y por perímetro**, no una vez para todo el sistema.

## 4. Policy Engine

Política = función que evalúa contexto y devuelve una decisión granular:

```
policy.evaluate(capability, resource, perimeter, mission_envelope, world)
  → PolicyDecision { verdict: allow | deny | require_approval | propose
                     reason, matched_rule, confidence }
```

- Las reglas son **datos** (JSON/DB), no condiciones hardcodeadas en el runtime.
- Evaluación ordenada: reglas del envelope (más específicas) → reglas globales.
- Toda evaluación se registra en audit (`policy.evaluated`, con `matched_rule`).
- El `AutonomyGate` existente (ASSIST/SUPERVISED/AUTONOMOUS) sigue siendo quien
  aplica el resultado con confianza (MetaCognition), pero ahora **consume** `PolicyDecision`
  granular por capacidad en lugar de listas planas de acciones.

Matrices de ejemplo (el objetivo, no la implementación actual):

**Tarea de análisis**
```
fs.read        → allow (auto)
git read       → allow (auto, si capability habilitada)
browser research → allow (si capability habilitada) | propose | deny por regla
fs.write       → no está en el plan (planner no la elige; si se intenta → deny "no requerida")
```

**Tarea de corrección**
```
fs.read        → auto
fs.write       → allow dentro del perímetro del proyecto
tests          → auto dentro del sandbox
delete         → approval si es necesario (no siempre: depende de la regla)
git commit     → approval según política (p.ej. solo mensaje firmado)
git push       → approval (humano o delegación explícita)
```

**Tarea de investigación web**
```
browser/web    → permitido mediante la capability correspondiente
acceso arbitrario desde el sandbox → NO necesariamente permitido
```

## 5. Mission Envelope v2

El envelope deja de ser "una lista de acciones" y pasa a declarar alcance:

```yaml
mission:
  objective: "Investiga el bug y aplica la solución"
  capabilities: [ fs.read, git.read, fs.write, git.commit, test ]
  perimeters:
    - project: hospitality-os
    - browser: { domains: [github.com, docs.example.com] }
  forbidden:
    - fs.remove
    - git.push
  approval_required: [ destructive, external_communication ]
  auto_approve: []            # delegación explícita de approval para Ciertos casos
  resource_budget: { max_runtime_minutes: 480, max_cost_usd: 10 }
  autonomy: autonomous
```

El envelope es la **declaración del usuario**; si una capacidad no está en el
envelope, el paso que la pida → `deny` honesto (BLOCKED), no aprobación.

## 6. Planeación dinámica

El flujo fijo `understand → research → execute → verify` desaparece como secuencia
obligatoria. El plan es un **grafo acíclico de etapas opcionales**; el planner elige
los pasos según objetivo + contexto + capacidades habilitadas. `understand`, `browser`,
`observe`, `synthesize`, `modify`, `test`, `verify`, `answer` son **etapas posibles**,
no una plantilla.

```
"¿Qué es X?"                        → understand → answer
"Investiga X."                      → understand → browser → observe → synthesize → answer
"Revisa mi proyecto."               → understand → filesystem/git → analyze → verify → answer
"Corrige el error y prueba."        → understand → inspect → plan → modify → test → observe
                                      → replan si falla → verify → answer
"Investiga y modifica si hay solución"
                                   → research → evidence → plan → approval si procede
                                      → modify → test → verify → answer
```

- Cada paso declara la `capability` que necesita (p.ej. `fs.write`,
  `browser.research`).
- El runtime pide policy por paso; por eso la cola (`mission.queue`) y los gates ya
  están preparados para procesar pasos heterogéneos y propósito de `approve`/`deny`
  sobre el paso pendiente.
- El plan persiste (`context["plan_steps"]` + `Mission.plan`), así un replan si
  ocurre, no reapila etapas ya completadas.

## 7. Qué evoluciona en el código actual

| Fichero | Rol hoy | Evolución |
|---|---|---|
| `alexis/contracts.py` | Envelope con `allowed_actions` planas | `MissionEnvelope` ampliado: `capabilities`, `perimeters`, `auto_approve`; tipos `Capability`, `PolicyDecision` granular, `Approval` |
| `alexis/security/policy.py` | Listas + riesgo hardcodeado (`HIGH→approval`) | `PolicyEngine` por reglas (datos): evalúa `(capability, recurso, perímetro, contexto)`; `matched_rule` en audit; override por envelope |
| `alexis/tools/registry.py` | `Tool` con `risk/permissions` planos | Tool vinculado a `capability_id` + `sandbox_profile` + red; registry habilitable/deshabilitable por política; expose catálogo |
| `alexis/tools/filesystem.py` | `classify_objective_intent` como "fuente única de permisos" | Se vuelve un **oráculo de intención** (input); la decisión la toma la policy; el workspace pasa a ser el perímetro `sandbox-project` |
| `alexis/security/sandbox.py` | Único `SandboxRunner` (workspace, sin red) | Perfiles de sandbox por capacidad (`sandbox-project`, `browser-sandbox`, `network-observed`, `host-delegated`); mismos RLIMIT/timeout/env limpio |
| `alexis/execution.py` | `ACTION_TOOL` fijo (`research→fs.read`…); "unsupported" hardcodeado | Dispatch por capacidad: el paso declara la capability y el executor resuelve la tool habilitada; "no soportado" → "capacidad no habilitada para este envelope" |
| `alexis/cognition/planner.py` | Rutas fijas (activation/desktop/fs) con secuencia determinista | Planner por capacidades: DAG de etapas opcionales; `plan_to_dict/by_dict` ya listos para persistencia |
| `alexis/autonomy/gates.py` *(nuevo)* | Decide por nivel + envelope + confianza | Consumir `PolicyDecision` granular por capacidad; capacidades como input; sigue auditando `reason` |
| `alexis/autonomy/queue.py` *(nuevo)* | FIFO + reanudar + plan persistido | Pausa/reanuda por aprobación, colas por prioridad, stop por política |
| `alexis/verification.py` | Verificadores fs/desktop/activación | Verificadores por dominio (git diff, tests, evidence web, screenshots) elegidos por el plan |
| `apps/demo/server.py` | Envelope con `allowed_actions` fija en el POST | Envelope construido por perfil de misión; `GET /capabilities`; UI muestra decisiones/plan |
| `alexis/storage/schema.py` + `repositories.py` | tables missions/tasks/… | `policy_rules`, `capability_registry`, `approvals` |
| `tests/` | Asumen flujo y espacio único | Tests por matriz capacidad×riesgo×nivel; los 114 verdes se conservan |

**Nuevos módulos** (fase de implementación, no ahora):
`alexis/capabilities/` → `catalog.py`, `profiles.py`, `external.py`
(adaptadores con contrato `connect / invoke / trust_domain / close`);
`alexis/security/policy_rules.py` → reglas como datos + DSL/JSON.

## 8. Roadmap de implementación (por fases, cada una con su puerta)

No se implementa todo hoy; el orden respeta seguridad y verificabilidad.

Esta línea se numera **C1–C5** (familia de capacidades). Los nombres `F0–F5+` quedan
reservados para la línea de inteligencia (F0 Self Model, F1 Capabilities foundation,
F2 Cognitive Core), aprobada en `docs/COGNITIVE-CORE-F2.md §0`.

- **C1 — Capability Foundation**: `CapabilitySpec` + Registry + `PolicyEngine` por
  reglas + planner por DAG de etapas, suficiente para las capacidades ya existentes
  (fs, desktop, tts) y añadir terminal/git local en `sandbox-project`. Puerta: matriz
  de tests capacidad×riesgo×nivel. **Completado.**
- **C2 — Web Research**: `browser-sandbox` headless, evidence (fuente, confianza) y
  verificación de investigación. Puerta: investigación web end-to-end sin llaves y sin
  tocar el proyecto.
- **C3 — External Integrations**: adaptadores Obsidian, OmniRoute, Hermes, Codex/
  coding agents, OpenCode, MCP, APIs; cada uno con spec, trust y sandbox propios.
- **C4 — Advanced Capabilities**: verificación por dominio (git diff, tests),
  capacidades de lectura de proyecto más allá del workspace, y percepción de pantalla
  (`vision.screen`) cuando exista adaptador real.
- **C5 — Environment / IoT / etc.**: MQTT/Home Assistant con políticas de actuadores,
  y el resto de dependencias del entorno físico (sensores, actuators).

Ningún provider externo (OmniRoute, Codex, OpenCode, Hermes, MCP…) es el cerebro de
ALEXIS: son **adapters/providers** plugged en el Core. El Core no depende de ninguno.

## 9. Invariantes que NO cambian (seguridad)

1. ALEXIS no puede auto-otorgarse capacidades ni modificar sus propias políticas.
2. Una misión no puede exceder su envelope (capacidad fuera → deny).
3. Las credenciales son referencias, nunca contexto por defecto.
4. Contenido no confiable = datos, no instrucciones (defensa ante prompt injection).
5. Operaciones de alto impacto: approval salvo delegación explícita en el envelope.
6. Verificación independiente para operaciones consecuentes.
7. Auditoría de cada decisión de política (`matched_rule`, razón, resultado).
8. Más autonomía ⇒ más evidencia y más verificación (MetaCognition → confianza).

La seguridad no se relaja. Lo que cambia es el grano: de restricción global a
**decisión contextual y granular** por capacidad, gobernada por política, aislada en
sandbox, acotada por envelope y vigilada por auditoría y verificación.