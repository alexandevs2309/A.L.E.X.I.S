import asyncio
import os
import resource
import time
from dataclasses import dataclass
from pathlib import Path


class SandboxError(Exception):
    """Intento de salir de la sandbox o fallo de ejecución controlado."""


@dataclass
class SandboxResult:
    ok: bool
    stdout: str
    stderr: str
    returncode: int | None
    timed_out: bool
    elapsed_ms: float


def _apply_limits():
    """Límites en el proceso hijo antes de exec (posix)."""
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_FSIZE, (8 * 1024 * 1024, 8 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))
    resource.setrlimit(resource.RLIMIT_CPU, (8, 8))
    resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
    try:
        resource.setrlimit(resource.RLIMIT_NPROC, (16, 16))
    except (ValueError, OSError):
        pass


class SandboxRunner:
    """subprocess restringido: cwd acotado al workspace, env limpio, RLIMIT, timeout.

    Sin red: no se crean sockets ni se pasa credencial alguna; la lectura/escritura
    se hace en el workspace local. El bloqueo completo de red requiere namespaces
    con privilegios (fase futura) y queda documentado como límite honesto.
    """

    def __init__(self, workspace: str | Path, timeout: float = 5.0, max_output: int = 64 * 1024):
        self.workspace = Path(workspace).resolve()
        self.timeout = timeout
        self.max_output = max_output

    def resolve_in_workspace(self, relative: str | Path) -> Path:
        candidate = (self.workspace / relative).resolve() if not Path(relative).is_absolute() else Path(relative).resolve()
        if not self._inside(candidate):
            raise SandboxError(f"path fuera del workspace: {candidate}")
        return candidate

    def _inside(self, path: Path) -> bool:
        try:
            path.relative_to(self.workspace)
        except ValueError:
            return False
        return True

    async def run(
        self,
        command: list[str],
        cwd: str | Path | None = None,
        timeout: float | None = None,
        env: dict | None = None,
    ) -> SandboxResult:
        if not command:
            raise SandboxError("comando vacío")
        workdir = self.resolve_in_workspace(cwd if cwd is not None else ".")
        if not workdir.exists():
            raise SandboxError(f"cwd inexistente: {workdir}")

        clean_env = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": str(self.workspace),
            "LANG": "C.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        if env:
            clean_env.update(env)

        start = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(workdir),
            env=clean_env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            preexec_fn=_apply_limits,
        )
        limit = timeout if timeout is not None else self.timeout
        timed_out = False
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=limit)
        except asyncio.TimeoutError:
            timed_out = True
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            out, err = await proc.communicate()
        elapsed = (time.monotonic() - start) * 1000

        return SandboxResult(
            ok=(not timed_out and proc.returncode == 0),
            stdout=out[: self.max_output].decode("utf-8", "replace"),
            stderr=err[: self.max_output].decode("utf-8", "replace"),
            returncode=proc.returncode,
            timed_out=timed_out,
            elapsed_ms=round(elapsed, 2),
        )
