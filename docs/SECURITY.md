# ALEXIS Security Model

> **Evolución (v0.5):** la seguridad pasa de restricciones globales (un workspace,
> sin red, sin herramientas externas, borrar siempre con aprobación) a un modelo
> **decisión contextual y granular**: CAPABILITY / POLICY / APPROVAL / SANDBOX /
> MISSION ENVELOPE (`docs/AUTONOMY-V0.5-CAPABILITIES.md`). Los invariantes de abajo
> no cambian: lo que cambia es el grano y la configurabilidad, no la obligación.

## Non-negotiable rules

1. ALEXIS cannot grant itself new authority.
2. A mission cannot exceed its envelope.
3. Credentials are references, never model context by default.
4. Destructive operations require explicit authorization.
5. Production deployment requires explicit authorization unless separately delegated.
6. External communication requires policy authorization.
7. Financial actions require explicit authorization.
8. Tools run with least privilege.
9. Untrusted content is data, not instructions.
10. Important actions require independent verification.

## Risk classes

- LOW: read-only inspection, local analysis.
- MEDIUM: file modifications, commits, non-production service changes.
- HIGH: external communication, infrastructure changes, production operations.
- CRITICAL: destructive, financial, credential, security-boundary changes.

> Las clases de riesgo alimentan la **Policy Engine**: la capacidad + su riesgo +
> el contexto + el envelope producen `allow | deny | require_approval | propose`.
> "Destructivo" no implica siempre aprobación: implica que la **política** lo exija
> (o que el envelope lo declara en `approval_required`).

## Prompt injection defense

Tool outputs, web pages, repository files and documents are untrusted observations. They must not be allowed to redefine ALEXIS policy or mission permissions.

## Audit

Every consequential action should record:
mission_id, task_id, actor, tool, arguments hash, authorization, start/end time, result, verification and rollback reference.
