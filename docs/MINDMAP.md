# ALEXIS — Mapa Mental Maestro

```text
ALEXIS
│
├── 1. ALEXIS CORE
│   ├── Identity
│   ├── Self Model (operacional + presencia; ver `SELF-MODEL.md`)
│   ├── Runtime
│   ├── Goal Management
│   ├── Decision Engine
│   └── Orchestrator
│
├── 2. COGNITION
│   ├── Reasoning
│   ├── Planning
│   ├── Decomposition
│   ├── Reflection
│   ├── Meta-cognition
│   ├── Confidence
│   └── Uncertainty
│
├── 3. AUTONOMY
│   ├── Mission Engine
│   ├── Initiative Engine
│   ├── Scheduler
│   ├── Long-running Tasks
│   ├── Checkpoints
│   ├── Recovery
│   └── Stop Conditions
│
├── 4. MEMORY
│   ├── Episodic
│   ├── Semantic
│   ├── Procedural
│   ├── Project Context
│   ├── Preferences
│   ├── Knowledge Graph
│   └── Vector Memory
│
├── 5. WORLD MODEL
│   ├── Person
│   ├── Projects
│   ├── Repositories
│   ├── Servers
│   ├── Databases
│   ├── Services
│   ├── Devices
│   └── Dependencies
│
├── 6. PERCEPTION
│   ├── Text
│   ├── Vision
│   ├── Screen
│   ├── Audio
│   ├── Camera
│   └── IoT Sensors
│
├── 7. ACTION / EXECUTION
│   ├── Tool Registry
│   ├── Terminal
│   ├── Filesystem
│   ├── Git / GitHub
│   ├── Browser
│   ├── APIs
│   ├── Database
│   ├── Cloud
│   └── IoT Actuators
│
├── 8. MULTI-AGENT FABRIC
│   ├── Researcher
│   ├── Coder
│   ├── QA
│   ├── Security
│   ├── DevOps
│   ├── Browser
│   ├── Data
│   ├── IoT
│   └── Critic / Verifier
│
├── 9. LEARNING
│   ├── Experience
│   ├── Evaluation
│   ├── Reflection
│   ├── Skill Factory
│   ├── Skill Versioning
│   ├── Experiments
│   ├── Prediction
│   ├── ML
│   ├── Fine-tuning
│   └── Optional RL
│
├── 10. SECURITY
│   ├── Mission Envelope
│   ├── RBAC / Permissions
│   ├── Secrets
│   ├── Sandbox
│   ├── Prompt Injection Defense
│   ├── Audit
│   ├── Resource Budgets
│   ├── Approval Gates
│   └── Rollback
│
├── 11. MODEL FABRIC
│   ├── Model Router
│   ├── Local Models
│   ├── Cloud Models
│   ├── Embeddings
│   ├── Vision Models
│   └── Specialized Models
│
├── 12. COMMUNICATION
│   ├── Voice
│   ├── STT
│   ├── TTS
│   ├── Chat
│   ├── Notifications
│   ├── Email
│   └── Mobile
│
├── 13. EXPERIENCE ENGINE
│   ├── Futuristic Avatar
│   ├── 3D / WebGL
│   ├── Dynamic UI
│   ├── Mission Visualization
│   ├── Code / Terminal Views
│   ├── Charts / Maps
│   ├── Voice Presence
│   └── Mobile Experience
│
└── 14. OBSERVABILITY
    ├── Events
    ├── Logs
    ├── Metrics
    ├── Traces
    ├── Mission Timeline
    ├── Audit Trail
    └── Health / Watchdog
```

> **Separación de planos:** WORLD MODEL (5) = mundo externo; SELf MODEL (1) = ALEXIS
> misma (estado, capacidades, permisos, confianza, lecciones); MEMORY (4) =
> persistencia de experiencias. Presence deriva su estado del Self Model real, no
> de simulaciones paralelas (detalle en `docs/SELF-MODEL.md`).

## Flujo maestro

```text
USER / EVENT / SENSOR
        │
        ▼
   PERCEPTION
        │
        ▼
   CONTEXT ENGINE
        │
        ▼
    WORLD MODEL
        │
        ▼
      GOAL
        │
        ▼
  MISSION ENGINE
        │
        ▼
      PLAN
        │
        ▼
   POLICY ENGINE
        │
        ├── necesita autorización ──► HUMAN
        │
        ▼
   AGENT / TOOL
        │
        ▼
    EXECUTION
        │
        ▼
   OBSERVATION
        │
        ▼
   VERIFICATION
        │
        ├── falla ──► RECOVERY / REPLAN
        │
        ▼
      RESULT
        │
        ▼
     MEMORY
        │
        ▼
     LEARNING
        │
        ▼
  EXPERIENCE ENGINE
```
