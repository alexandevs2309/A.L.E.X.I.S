# ALEXIS v0.3 — Autonomous Intelligence Foundation

**A.L.E.X.I.S. = Autonomous Learning, Execution & Intelligence System**

This version defines ALEXIS as a complete personal AI system rather than a chatbot or coding agent.

`docs/VISION.md` is the **north-star specification** for the complete system, and this
repository is an incremental, honest implementation of its foundation. Not everything in the
vision exists yet, and nothing here claims it does: `docs/VISION-ALIGNMENT.md` records what is
real, what is partial, and what is only a contract.

## Architecture

ALEXIS is organized around twelve capability domains:

1. Cognition — reasoning, planning, decisions and reflection.
2. Memory — episodic, semantic and procedural memory.
3. Perception — files, screens, images, audio and future sensors.
4. Autonomy — goals, missions, scheduling, initiative and recovery.
5. Action — tools, browser, terminal, Git, APIs and future IoT.
6. Agents — specialized workers coordinated by ALEXIS Core.
7. Learning — experience, skills, experiments and controlled improvement.
8. World Model — projects, infrastructure, devices, services and context.
9. Meta-cognition — confidence, uncertainty and evidence quality.
10. Communication — voice, chat, notifications and future channels.
11. Security — policy, authorization, sandboxing, audit and human approval.
12. Experience Engine — optional futuristic visual/voice presence.

## Core loop

Goal → Context → Plan → Policy → Execute → Observe → Evaluate → Replan → Verify → Commit → Learn

The Experience Engine is intentionally secondary: it visualizes real ALEXIS state and does not pretend to be the intelligence itself.

## Safety principles

- Autonomy governed by capabilities + policy (CAPABILITY / POLICY / APPROVAL / SANDBOX / MISSION ENVELOPE).
- Explicit mission envelope; capabilities outside the envelope are denied.
- Least privilege, now contextual per capability and perimeter.
- Human approval for destructive, external, financial or production actions (or explicit delegation in the envelope).
- Independent verification for consequential operations.
- Resource budgets.
- Audit trail (every policy decision recorded).
- Stop conditions.
- No unrestricted self-modification.

## Status

This is an architectural and executable foundation. Provider integrations, persistent infrastructure, real sandboxes, browser automation, STT/TTS, vision, IoT and production hardening are intentionally isolated behind interfaces.

## Running

El **runtime oficial de ALEXIS es `apps/demo`**. Es el único que ejecuta
`CognitiveRuntime` + `IntentClassifier` + catálogo de capacidades + `PolicyEngine` +
`AutonomyGate` + `SandboxExecutor` + `GoalVerifier`, y el único que persiste en
PostgreSQL. No hay un segundo runtime: `apps/api` es una fachada HTTP del mismo.

```bash
pip install -e .
python3 -m apps.demo.server            # o: make run     → http://127.0.0.1:8100
```

Superficie oficial: `GET /health`, `GET /ui`, `POST /chat`, `GET|POST /missions`,
`POST /missions/{id}/approve`, `POST /missions/{id}/clarify`, `POST /missions/{id}/deny`,
`GET /stream` (SSE del `EventBus` real), `GET /state`, `GET /self`, `GET /capabilities`.

Opcional: la fachada FastAPI, que **delega en el mismo runtime oficial** y exige
`X-ALEXIS-Token` fuera de development:

```bash
uvicorn apps.api.main:app --reload     # o: make run-api
```

`ALEXIS_COGNITIVE` ya no elige entre dos recorridos: el `CognitiveRuntime` es obligatorio.
Ponerla a `0` **no** arranca un modo alternativo —aborta el arranque, porque un runtime
sin `GoalVerifier` no puede cerrar ninguna misión (ver `apps/demo/runtime_flags.py`).

## Documentation map

**Start here if you are an agent or new contributor:**

- `docs/VISION.md` — **north-star**: the complete long-term ALEXIS. Describes the TARGET, not
  the current system. Read its warning block before anything else.
- `docs/VISION-ALIGNMENT.md` — **what actually exists**, section by section against the
  vision, with evidence pointers. Check this before claiming a capability works.

**Current state of the system:**

- `docs/IMPLEMENTATION-STATUS.md` — qué está implementado y qué falta.
- `docs/P0-COGNITIVE-CORE-GAP-ANALYSIS.md` — auditoría P0 con su método de puntuación.
- `docs/MINDMAP.md` — mapa mental maestro.
- `docs/BLUEPRINT.md` — blueprint arquitectónico.
- `docs/SYSTEM-MAP.md` — mapa de sistemas y estado conceptual.
- `docs/SKETCH.md` — boceto de la experiencia visual.
- `docs/CAPABILITIES.md` — mapa de capacidades.
- `docs/AUTONOMY-V0.5-CAPABILITIES.md` — modelo de autonomía por capacidades y políticas (v0.5).
- `docs/SELF-MODEL.md` — Self Model: autoconocimiento operacional de ALEXIS y presencia desde el estado real.
- `docs/DEVELOPMENT.md` — guía de desarrollo ordenada: por dónde empezar y cómo seguir.
- `docs/SECURITY.md` — modelo de seguridad.
- `docs/ROADMAP.md` — evolución por versiones.

**Importante:** v0.3 define el sistema completo y deja preparada la arquitectura; no pretende hacer pasar placeholders por capacidades productivas.
