# ALEXIS v0.3 Architecture

> **Modelo de autonomía actualizado:** ALEXIS es un sistema autónomo con
> **capacidades gobernadas por políticas**, no un sistema permanentemente
> restringido. Ver `docs/AUTONOMY-V0.5-CAPABILITIES.md` (CAPABILITY / POLICY /
> APPROVAL / SANDBOX / MISSION ENVELOPE).

## 1. System layers

### ALEXIS Core
Owns identity, goals, context, planning, policy decisions and orchestration.

### Mission Engine
Converts a user objective into a bounded autonomous mission.

### Execution Fabric
Runs tasks through specialized agents and tools with retries, leases, checkpoints and recovery.

### Intelligence Fabric
Model router + memory + knowledge + world model + meta-cognition.

### Learning Fabric
Experience → evaluation → reflection → skill candidate → tests → version.

### Experience Engine
Conversation-first UI and context-driven presentation. It consumes state/events from Core.
ALEXIS manifiesta su presencia mediante la interacción y el contexto, sin un objeto visual
permanente (el orb/dashboard fueron descartados como experiencia principal). Los datos
técnicos (JSON, inventarios, logs) viven en un «Modo Sistema» separado.

## 2. Mission envelope

El envelope declara **qué capacidades** puede usar la misión y **en qué perímetros**,
no solo "acciones". Se evalúa con la policy por paso (ver `AUTONOMY-V0.5-CAPABILITIES.md`).

```yaml
mission:
  objective: "Improve Hospitality OS performance"
  duration: "8h"
  allowed_projects:
    - hospitality-os
  allowed_actions:
    - read
    - research
    - modify
    - test
    - commit
  forbidden_actions:
    - production_deploy
    - delete_database
    - external_payment
  approval_required:
    - production
    - destructive
    - external_communication
  resource_budget:
    max_cost_usd: 10
    max_runtime_minutes: 480
```

## 3. State machine

PENDING → PLANNING → RUNNING → VERIFYING → COMPLETED

Exceptional paths:

RUNNING → WAITING_APPROVAL
RUNNING → BLOCKED
RUNNING → FAILED → RECOVERING → RUNNING
RUNNING → STOPPED

> El plan interno es un **grafo de etapas opcionales** (`understand`, `browser`,
> `observe`, `modify`, `test`, `verify`, `answer`…); el planner elige dinámicamente
> según objetivo y capacidades. No existe una secuencia obligatoria.

## 4. Agents

Agents are workers, not authorities. ALEXIS Core remains responsible for mission policy.

Suggested roles:
- researcher
- coder
- qa
- security
- devops
- browser
- data
- iot
- critic

## 5. World model

The world model represents:
- person and preferences
- projects
- repositories
- servers
- databases
- services
- devices
- credentials references
- active missions
- dependencies
- operational state

Secrets themselves never belong in the world model.

## 6. Self model

ALEXIS mantiene un **Self Model** operacional y persistente de sí misma: estado,
objetivo/misión actual, capacidades disponibles y requeridas, permisos y límites
vigentes, acciones recientes, decisiones, incertidumbres, confianza, aprobaciones
pendientes, errores, compromisos, resultados y lecciones. Se separa claramente de
**World Model** (mundo externo) y de **Memory** (persistencia de experiencias), y se
actualiza consumiendo los **eventos reales del runtime**, no ficheros JSON estáticos.
Presence consume `self.status` derivado del Self Model (una sola fuente de verdad, el
frontend solo dibuja). Ver `docs/SELF-MODEL.md` (zonas del modelo, ciclo
GOAL→SELF-REFLECTION, auto-preguntas, presencia y evolución del código).

## 7. Learning

Learning must be promoted through evaluation. A model-generated conclusion is not automatically a learned fact.

Experience record:
input → action → observation → outcome → evaluation → lesson → candidate skill → tests → version.

## 6. Learning

Learning must be promoted through evaluation. A model-generated conclusion is not automatically a learned fact.

Experience record:
input → action → observation → outcome → evaluation → lesson → candidate skill → tests → version.
