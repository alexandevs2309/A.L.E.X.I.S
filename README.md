# ALEXIS v0.3 — Autonomous Intelligence Foundation

**A.L.E.X.I.S. = Autonomous Learning, Execution & Intelligence System**

This version defines ALEXIS as a complete personal AI system rather than a chatbot or coding agent.

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

```bash
pip install -e .
uvicorn apps.api.main:app --reload     # o: make run
```

API: `GET /health`, `GET /ui` (interfaz en vivo), `POST /missions`, `GET /missions[/{id}]`, `POST /missions/{id}/run`, `POST /missions/{id}/approve`, `GET /stream` (Server-Sent Events del runtime real).

Alternativa sin dependencias (solo stdlib):

```bash
python3 -m apps.demo.server            # http://127.0.0.1:8100
```

## Documentation map

- `docs/MINDMAP.md` — mapa mental maestro.
- `docs/BLUEPRINT.md` — blueprint arquitectónico.
- `docs/SYSTEM-MAP.md` — mapa de sistemas y estado conceptual.
- `docs/SKETCH.md` — boceto de la experiencia visual.
- `docs/CAPABILITIES.md` — mapa de capacidades.
- `docs/AUTONOMY-V0.5-CAPABILITIES.md` — modelo de autonomía por capacidades y políticas (v0.5).
- `docs/SELF-MODEL.md` — Self Model: autoconocimiento operacional de ALEXIS y presencia desde el estado real.
- `docs/IMPLEMENTATION-STATUS.md` — qué está implementado y qué falta.
- `docs/DEVELOPMENT.md` — guía de desarrollo ordenada: por dónde empezar y cómo seguir.
- `docs/SECURITY.md` — modelo de seguridad.
- `docs/ROADMAP.md` — evolución por versiones.

**Importante:** v0.3 define el sistema completo y deja preparada la arquitectura; no pretende hacer pasar placeholders por capacidades productivas.
