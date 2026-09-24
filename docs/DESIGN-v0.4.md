# ALEXIS — Diseño v0.4 (Real Autonomous Runtime)

Basado en `docs/AUDIT-v0.3.md` y en las decisiones aprobadas el 2026-09-21.

## 1. Decisiones de arquitectura aprobadas

| ID | Decisión | Acuerdo |
|---|---|---|
| D1 | Estabilidad de infraestructura: volumen Postgres, puerto coherente **5433**, password vía env, `.dockerignore`, dev-deps | ✅ Aplicado en esta etapa |
| D2 | **Eventos durables en PostgreSQL** (outbox + LISTEN/NOTIFY). NATS solo cuando haya multi-worker | ✅ |
| D3 | **Sandbox = subprocess restringido** (stdlib): workspace autorizado, whitelist, RLIMIT, timeout. Docker como segunda capa en Fase posterior | ✅ |
| D4 | **Milestone 1 determinista**: sin LLM; `ModelRouter` queda como interfaz + adapter `echo` honesto. Las capacidades no se fingen | ✅ |
| D5 | **Auth mínima por token** (`ALEXIS_API_TOKEN`) en la API; bind `127.0.0.1` en dev | ✅ |

## 2. Criterios de fase (gate)

No se marca terminada una fase sin: código ✅ tests ✅ integración ✅ errores manejados ✅ seguridad ✅ observabilidad ✅ documentación ✅ migraciones ✅ recuperación ✅.

## 3. Milestone 1 (M1)

> **"Una misión persistente que puede ser creada, planificada, ejecutada, observada, verificada y finalizada"**, con tool real de bajo riesgo, verificación independiente y auditoría; sobrevive a reinicio desde el último checkpoint.

Flujo que debe funcionar de verdad (no simulado):

```
crear misión → persistir → plan → tasks queued → worker claim (lease)
→ ejecutar tool real (filesystem read en workspace) en subprocess restringido
→ execution + observation + evento durable
→ verificación independiente (regla simple: archivo existe / contenido esperado)
→ verification persistida → audit → close mission → memory de la experiencia
```

## 4. Estado objetivo (v0.4) por dominio

- **Contracts**: `Task`, `TaskState`, `Execution`, `Checkpoint`, `Approval`, `Artifact`.
- **Mission**: persistencia en cada transición de estado; `context`/`results` en DB.
- **Task Runtime**: estados `pending→queued→running→waiting_approval→completed/failed/cancelled/retrying`; lease, heartbeat, timeout, retry.
- **Scheduler**: inmediato / retrasado / recurrente; anti-duplicación, anti-órfanas.
- **Checkpoints + Recovery**: persistir `step_index`+payload; reanudar desde el último válido (no reiniciar la misión entera).
- **Tool Runtime**: `Tool` con `name/description/schema/permissions/risk/timeout/límites/audit`. Primera tool real: **filesystem read** dentro del workspace autorizado.
- **Sandbox**: `subprocess` con `cwd` acotado, `RLIMIT_*`, timeout, sin red.
- **Model Router**: interfaz + adaptadores; adapter `echo` (determinista) para M1.
- **Memory**: episódica sobre pgvector (A1 ya creó la extensión).
- **Verification**: clase independiente del executor; reglas deterministas primero.
- **Audit**: `AuditRepository` conectado al runtime.
- **Observability**: cada hito es un evento durable (topic `mission.*`, `task.*`, `tool.*`, `verification.*`).
- **CLI**: `python -m alexis mission create/run/status ...`.

## 5. Orden de implementación (M1 desglosado)

1. **S1 — Infra y contratos** (ESTA etapa): fixes de consistencia aprobados; contratos `Task/TaskState/Execution/Checkpoint`; schema v0.4 (tasks, executions, observations, verifications, checkpoints); repositorios; CLI `verify` ampliado. Migraciones idempotentes.
2. **S2 — Runtime persistente**: `AlexisRuntime` persiste la misión en cada transición; `MissionRepository` usado por la API/demo; cierre de misión escribiendo verification + audit.
3. **S3 — Task Runtime + Scheduler + Checkpoints + Recovery**: `TaskRunner`, leases/heartbeats con `lease_until`, reclamación de `queued`, `next_queued`, retry con `max_attempts`, checkpointing por `step_index`, `resume_from_checkpoint`.
4. **S4 — Tool filesystem read real + Sandbox subprocess**: `FileSystemReadTool` con `schema/permissions/risk/timeout`; `SandboxRunner` (subprocess, cwd=workspace, whitelist, RLIMIT, timeout, sin red); registrar en `ToolRegistry`; `ExecutionRepository` con `args_hash`.
5. **S5 — Verification determinista + Audit conectado**: `FilesystemVerifier` independiente (ej.: path permitido, existe, no se toca fuera del workspace); escribir `verifications` y `audit_log`.
6. **S6 — API/CLI + tests integración**: endpoints con persistencia real y token; `make demo`; tests de lifecycle, worker failure, timeout, forbidden tool, approval, checkpoint recovery, failed verification.

## 6. Archivos que tocará M1

`alexis/contracts.py`, `alexis/core/runtime.py`, `alexis/autonomy/` (nuevo `scheduler.py`, `task_runner.py`), `alexis/execution.py`, `alexis/verification.py`, `alexis/tools/filesystem.py` (nuevo), `alexis/security/sandbox.py` (nuevo), `alexis/storage/schema.py` + `repositories.py`, `apps/api/main.py`, `apps/demo/server.py`, `tests/*`, `docs/IMPLEMENTATION-STATUS.md`, `README.md`.

## 7. Fuera de alcance hasta fases futuras

Avatar/UI futura, voz/visión, IoT, browser, NATS, aprendizaje automático/RL, fine-tuning. (Directiva §9.)