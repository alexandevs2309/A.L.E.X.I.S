"""`terminal.run`: ejecución de comandos dentro del SandboxRunner existente.

Reutiliza RLIMIT, cwd acotado, env limpio, timeout y tope de salida de
`alexis/security/sandbox.py`. Nunca crea un segundo mecanismo de aislamiento: sólo
pasa `argv` derivado del comando (sin shell) al mismo sandbox.
"""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any

from alexis.security.sandbox import SandboxError, SandboxRunner
from alexis.tools.registry import Tool

#: Aceptadas: la tool no inventa otras llaves.
ALLOWED_ARGS = frozenset({"command", "timeout_seconds", "cwd"})
#: shell/env/args/.. no tienen cabida: shell=True jamás se usa y env queda fijo.
FORBIDDEN_ARGS = frozenset({"shell", "env", "args", "extra_args", "argv", "cmd"})

#: Verbos que mutan el filesystem de forma delicada: el resultado pide
#: verificación post-ejecución vía FilesystemVerifier.
DESTRUCTIVE_COMMANDS = frozenset({"rm", "rmdir", "del", "erase", "dd", "mkfs", "chmod", "chown"})
MAX_TIMEOUT = 300.0


def _is_destructive(argv: list[str]) -> bool:
    if not argv:
        return False
    first = Path(argv[0]).name
    return first in DESTRUCTIVE_COMMANDS


def _plan_command(args: dict[str, Any], *, workspace: Path, sandbox_timeout: float) -> dict[str, Any]:
    invalid = set(args) - set(ALLOWED_ARGS)
    if invalid:
        raise ValueError(f"argumentos no permitidos para terminal.run: {sorted(invalid)}")
    forbidden = set(args) & FORBIDDEN_ARGS
    if forbidden:
        raise ValueError(f"terminal.run no acepta shell/env/args: {sorted(forbidden)}")

    command = args.get("command")
    if not isinstance(command, str) or not command.strip():
        raise ValueError("'command' debe ser una cadena no vacía")

    try:
        argv = shlex.split(command)
    except ValueError as exc:
        raise ValueError(f"comando inválido: {exc}") from exc
    if not argv:
        raise ValueError("comando vacío tras tokenizar")

    raw_cwd = args.get("cwd")
    if raw_cwd is None:
        resolved = workspace
    else:
        candidate = Path(raw_cwd)
        resolved = (workspace / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
        if not _inside_workspace(resolved, workspace):
            raise ValueError(f"cwd '{resolved}' fuera del workspace")

    t = args.get("timeout_seconds", sandbox_timeout)
    if isinstance(t, bool) or not isinstance(t, (int, float)):
        raise ValueError("'timeout_seconds' debe ser int|float")
    timeout = max(1.0, min(MAX_TIMEOUT, float(t)))

    return {
        "argv": argv,
        "cwd": str(resolved),
        "timeout": timeout,
        "destructive": _is_destructive(argv),
    }


def _inside_workspace(candidate: Path, workspace: Path) -> bool:
    try:
        candidate.relative_to(workspace.resolve())
        return True
    except ValueError:
        return False


class TerminalRunTool:
    name = "terminal.run"
    description = (
        "Ejecuta un comando de terminal arbitrario dentro del sandbox del proyecto: "
        "cwd acotado al workspace, env limpio, RLIMIT, timeout real (SIGTERM→kill), "
        "stdout/stderr truncados a 64KB, nunca shell=True. args: command, "
        "timeout_seconds?, cwd?. risk=high y requires_approval=true."
    )
    risk = "high"
    capability_id = "terminal.run"
    sandbox_profile = "terminal"

    def __init__(self, workspace: str | Path, *, sandbox: SandboxRunner | None = None, default_timeout: float = 30.0):
        self.workspace = Path(workspace).resolve()
        self.sandbox = sandbox or SandboxRunner(self.workspace, timeout=float(default_timeout))
        self.timeout = float(default_timeout)
        self.permissions = {
            "risk": "high",
            "requires_approval": True,
            "network": False,
            "allowed_directories": [str(self.workspace)],
        }
        self.limits = {
            "timeout": self.timeout,
            "max_output_bytes": getattr(self.sandbox, "max_output", 64 * 1024),
        }
        self.schema = {
            "command": {"type": "string", "description": "comando a ejecutar (argv, sin shell)"},
            "timeout_seconds": {"type": "number", "description": "timeout real (default 30; min 1; max 300)"},
            "cwd": {"type": "string", "description": "cwd dentro del workspace (default: raíz del workspace)"},
        }
        self.handler = self._run

    async def _run(self, args: dict[str, Any]) -> dict[str, Any]:
        try:
            plan = _plan_command(args, workspace=self.workspace, sandbox_timeout=self.timeout)
        except ValueError as exc:
            return {"ok": False, "error": str(exc), "stderr": "", "returncode": None}
        try:
            result = await self.sandbox.run(
                plan["argv"],
                cwd=plan["cwd"],
                timeout=plan["timeout"],
            )
        except SandboxError as exc:
            return {"ok": False, "error": str(exc), "stderr": "", "returncode": None}
        return {
            "ok": result.ok,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "returncode": result.returncode,
            "timed_out": result.timed_out,
            "destructive": plan["destructive"],
            # Pista operativa para FilesystemVerifier: si el comando es destructivo, la
            # evidencia de éxito no vale sin una observación post-ejecución adicional.
            "requires_post_execution_verification": plan["destructive"],
        }


def build_terminal_tools(workspace: str | Path, **over) -> list[Tool]:
    """Registra `terminal.run` lista para uso."""
    tool = TerminalRunTool(workspace, **over)
    return [
        Tool(
            name=tool.name,
            description=tool.description,
            risk=tool.risk,
            handler=tool.handler,
            schema=tool.schema,
            permissions=tool.permissions,
            timeout=tool.timeout,
            limits=tool.limits,
            capability_id=tool.capability_id,
            sandbox_profile=tool.sandbox_profile,
        )
    ]
