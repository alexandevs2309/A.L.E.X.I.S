# ALEXIS Capability Map

> **v0.5:** cada dominio de este mapa se despliega como **CapabilitySpec** concreto
> (id, esfera, red, side_effects, trust_domain, sandbox_profile, riesgo, auditoría)
> gobernado por la Policy Engine. Ver `docs/AUTONOMY-V0.5-CAPABILITIES.md`.

## Cognitive
Reasoning, planning, decision decomposition, reflection, uncertainty and verification.

## Autonomous
Mission execution, scheduling, recovery, initiative, stop criteria and long-running work.

## Digital perception
Files, repositories, APIs, logs, databases, browser, screens and images.

## Physical perception
Future cameras, microphones, sensors and IoT telemetry.

## Action
Filesystem, terminal, Git/GitHub, browser, APIs, cloud services and IoT.

## Multi-agent
Researcher, coder, QA, security, DevOps, browser, IoT and critic.

## Memory
Episodic, semantic, procedural, project context and knowledge graph.

## Learning
Experience evaluation, reflection, skill generation, experiments and controlled versioning.

## Communication
Voice, text, notifications and future mobile channels.

## Experience
Dynamic UI, avatar, 3D visualization, voice presence and context-aware visual surfaces.

## Physical world
MQTT, Home Assistant, sensors and actuators with explicit safety policies.

## Future intelligence
Local/cloud model routing, embeddings, multimodal models, ML, fine-tuning and reinforcement learning.
## Boundaries

ALEXIS does not get unlimited authority, unrestricted self-modification, or automatic permission to perform high-impact actions.

La seguridad es **contextual y granular**, no una restricción global: cada capacidad
tiene su política, su sandbox, su riesgo y su necesidad de aprobación evaluada en
tiempo de ejecución.
