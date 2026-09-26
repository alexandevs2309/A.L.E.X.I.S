"""P0 §5.4 — `execute.test`: ejecutar tests REALES del proyecto y sacar evidencia.

El catálogo declaraba `execute.test` desde el principio pero no tenía adaptador, así que
el `GoalVerifier` de §5.3 no podía comprobar un criterio como "los tests pasan". Esto es
esa capacidad, y es la primera que ejecuta código del proyecto.

Decisiones de seguridad, todas deliberadas:

- **argv fijo, nunca shell.** Se llama a `asyncio.create_subprocess_exec` con una lista.
  No hay `shell=True`, ni `bash -c`, ni concatenación de cadena. La inyección de shell no
  es "prevenida": es estructuralmente imposible por la vía de ejecución.
- **Argumentos estructurados y en lista blanca.** El modelo elige la capacidad y da
  campos cerrados (`path`, `runner`, `selectors`, `timeout`). Cualquier clave desconocida
  —`command`, `shell`, `args`, `extra_args`— se rechaza antes de hacer nada.
- **Selectores validados.** Cada node id debe casar con un patrón estricto y no puede
  contener metacaracteres de shell. Es defensa en profundidad: el ExecFile ya no
  interpretaría nada, pero la puerta existe y está probada.
- **Dentro del sandbox que ya existe.** Se reutiliza `SandboxRunner`: cwd acotado al
  workspace, env limpio y fijo, RLIMIT (CPU, AS, FSIZE, NOFILE, NPROC), timeout y tope de
  salida. No se crea un segundo mecanismo de aislamiento.
- **Riesgo declarado `medium`**, no `low`: ejecutar la suite ejecuta código de terceros.
  La aprobación la resuelve Policy según el riesgo del paso (high/critical), sin tocar
  las reglas.

Sobre `ok` y el significado de la evidencia, que es donde está la trampa:

`ok=True` significa **la suite se ejecutó y terminó con exit code 0**. `ok=False` con
`tests_failed > 0` significa que la suite corrió y falló: la acción se hizo, y el
resultado es el que es. Los conteos, `exit_code`, `stdout` y `stderr` viaja siempre, tanto
en `ok=True` como en `ok=False`, para que la evidencia exista aunque el paso haya fallado.

Que la suite pase NO verifica el objetivo. Eso lo decide el `GoalVerifier` de §5.3
evaluando el criterio; aquí solo se produce la observación real.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

from alexis.security.sandbox import SandboxError, SandboxResult, SandboxRunner
from alexis.tools.registry import Tool

#: Únicos runners permitidos. Añadir uno es una decisión de seguridad, no un argumento.
ALLOWED_RUNNERS = ("pytest",)

#: Claves aceptadas. Todo lo demás se rechaza: aquí no hay "flags extra".
ALLOWED_KEYS = frozenset({"path", "runner", "selectors", "timeout"})

#: Claves que se rechazan siempre, con nombre explícito para que el error sea legible.
FORBIDDEN_KEYS = frozenset({"command", "cmd", "shell", "script", "args", "extra_args", "env"})

#: Metacaracteres de shell. Nunca se interpretan (no hay shell), pero se rechazan.
#: Metacaracteres de shell. Nunca se interpretan (no hay shell), pero se rechazan.
#: Los corchetes NO están aquí: son legítimos en los ids parametrizados de pytest.
SHELL_METACHARACTERS = frozenset(";&|<>$`\n\r\t\\\"'(){}!*?~")

#: Node id de pytest: `ruta/fichero.py::test_nombre[param]`. Sin comillas ni espacios
#: exóticos, sin redirecciones, sin traversing.
SELECTOR_PATTERN = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./\-]*\.py(?:::[A-Za-z0-9_\-. \[\]]+)?$")

MAX_SELECTORS = 20
MIN_TIMEOUT = 1.0
MAX_TIMEOUT = 300.0

_COUNT_PATTERN = re.compile(r"(\d+)\s+(passed|failed|error|errors|skipped|xfailed|xpassed|deselected)")


class TestRunnerTool:
    """Ejecuta la suite de tests del workspace con pytest dentro del sandbox."""

    name = "execute.test"
    description = (
        "Ejecuta tests reales del proyecto con pytest y devuelve exit code, conteos de "
        "tests y la salida. No acepta comandos: solo un path del workspace, un runner y "
        "selectors de test."
    )
    risk = "medium"
    capability_id = "execute.test"
    sandbox_profile = "sandbox-project"

    def __init__(self, workspace: str | Path, *, sandbox: SandboxRunner | None = None, default_timeout: float = 120.0):
        self.workspace = Path(workspace).resolve()
        self.sandbox = sandbox or SandboxRunner(self.workspace)
        self.timeout = float(default_timeout)
        self.permissions = {
            "workspace": str(self.workspace),
            "execute": True,
            "write": False,
            "network": False,
            "shell": False,
        }
        self.limits = {
            "timeout": self.timeout,
            "max_selectors": MAX_SELECTORS,
            "max_output_bytes": getattr(self.sandbox, "max_output", 64 * 1024),
        }
        self.schema = {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Ruta relativa dentro del workspace con el proyecto a testear.",
                },
                "runner": {
                    "type": "string",
                    "enum": list(ALLOWED_RUNNERS),
                    "description": "Runner permitido. Solo pytest.",
                },
                "selectors": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": MAX_SELECTORS,
                    "description": "Node ids de pytest, p.ej. tests/test_x.py::test_y.",
                },
                "timeout": {
                    "type": "number",
                    "minimum": MIN_TIMEOUT,
                    "maximum": MAX_TIMEOUT,
                    "description": "Segundos máximos antes de matar la suite.",
                },
            },
            "required": [],
            "additionalProperties": False,
        }
        self.handler = self._run_tests

    # ------------------------------------------------------------------ #
    # Handler
    # ------------------------------------------------------------------ #

    async def _run_tests(self, args: dict) -> dict[str, Any]:
        try:
            plan = self._validate(args or {})
        except _Rejected as exc:
            return {"ok": False, "error": str(exc), "rejected": True, **exc.detail}

        argv = self._argv(plan["selectors"])
        try:
            result = await self.sandbox.run(
                argv,
                cwd=plan["path"],
                timeout=plan["timeout"],
            )
        except SandboxError as exc:
            return {"ok": False, "error": f"sandbox: {exc}", "command": argv, "cwd": str(plan["resolved"])}
        return self._payload(result, argv, plan)

    # ------------------------------------------------------------------ #
    # Validación: la puerta anti-inyección
    # ------------------------------------------------------------------ #

    def _validate(self, args: dict) -> dict[str, Any]:
        if not isinstance(args, dict):
            raise _Rejected("los argumentos deben ser un objeto")

        for key in sorted(args):
            if key in FORBIDDEN_KEYS:
                raise _Rejected(
                    f"argumento prohibido '{key}': esta capability no ejecuta comandos arbitrarios",
                    detail={"forbidden_key": key},
                )
        unknown = sorted(set(args) - ALLOWED_KEYS)
        if unknown:
            raise _Rejected(
                f"argumento desconocido: {', '.join(unknown)}. Permitidos: {', '.join(sorted(ALLOWED_KEYS))}",
                detail={"unknown_keys": unknown},
            )

        runner = str(args.get("runner", ALLOWED_RUNNERS[0])).strip()
        if runner not in ALLOWED_RUNNERS:
            raise _Rejected(
                f"runner no permitido: {runner!r}. Permitidos: {', '.join(ALLOWED_RUNNERS)}",
                detail={"runner": runner},
            )

        path = args.get("path", ".")
        if not isinstance(path, str) or not path.strip():
            raise _Rejected("'path' debe ser una cadena no vacía")
        path = path.strip()
        if ".." in path.split("/") or path.startswith("~"):
            raise _Rejected(f"path no permitido: {path!r}", detail={"path": path})
        try:
            resolved = self.sandbox.resolve_in_workspace(path)
        except SandboxError as exc:
            raise _Rejected(str(exc), detail={"path": path}) from exc
        if not resolved.exists():
            raise _Rejected(
                f"el path no existe en el workspace: {resolved.name}",
                detail={"path": str(resolved)},
            )
        if not resolved.is_dir():
            raise _Rejected(f"'path' debe ser un directorio: {path!r}", detail={"path": path})

        timeout = args.get("timeout", self.timeout)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise _Rejected(f"'timeout' debe ser un número, no {type(timeout).__name__}")
        timeout = max(MIN_TIMEOUT, min(MAX_TIMEOUT, float(timeout)))

        selectors = self._validate_selectors(args.get("selectors"))

        return {
            "path": path,
            "resolved": resolved,
            "runner": runner,
            "selectors": selectors,
            "timeout": timeout,
        }

    def _validate_selectors(self, raw) -> list[str]:
        if raw is None:
            return []
        if not isinstance(raw, list):
            raise _Rejected(f"'selectors' debe ser una lista, no {type(raw).__name__}")
        if len(raw) > MAX_SELECTORS:
            raise _Rejected(
                f"demasiados selectors: {len(raw)} (máximo {MAX_SELECTORS})",
                detail={"selectors": len(raw)},
            )
        selectors: list[str] = []
        for item in raw:
            if not isinstance(item, str):
                raise _Rejected(f"selector no válido: {item!r} (debe ser texto)")
            selector = item.strip()
            if not selector:
                raise _Rejected("selector vacío")
            bad = sorted(set(selector) & SHELL_METACHARACTERS)
            if bad:
                raise _Rejected(
                    f"selector con metacarácter de shell: {selector!r} ({', '.join(bad)})",
                    detail={"selector": selector, "metacharacters": bad},
                )
            if not SELECTOR_PATTERN.match(selector):
                raise _Rejected(
                    f"selector no válido: {selector!r}. Formato: ruta/test_x.py::test_nombre",
                    detail={"selector": selector},
                )
            selectors.append(selector)
        return selectors

    # ------------------------------------------------------------------ #
    # argv y payload
    # ------------------------------------------------------------------ #

    @staticmethod
    def _argv(selectors: list[str]) -> list[str]:
        """argv fijo. La estructura la pone el código, no el modelo.

        `--no-header` y `-p no:cacheprovider` son para que la salida sea parseable y para
        que la suite no escriba caché en el workspace.
        """
        argv = [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "--no-header",
            "-p",
            "no:cacheprovider",
        ]
        argv.extend(selectors)
        return argv

    def _payload(self, result: SandboxResult, argv: list[str], plan: dict[str, Any]) -> dict[str, Any]:
        counts = parse_pytest_counts(result.stdout)
        passed = counts.get("passed", 0)
        failed = counts.get("failed", 0) + counts.get("error", 0) + counts.get("errors", 0)
        payload: dict[str, Any] = {
            "ok": (not result.timed_out) and result.returncode == 0,
            "runner": plan["runner"],
            "command": argv,
            "cwd": str(plan["resolved"]),
            "path": plan["path"],
            "exit_code": result.returncode,
            "timed_out": result.timed_out,
            "duration_ms": result.elapsed_ms,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "tests_passed": passed,
            "tests_failed": failed,
            "tests_skipped": counts.get("skipped", 0),
            "counts_parsed": bool(counts),
            "selectors": list(plan["selectors"]),
            "timeout": plan["timeout"],
            # Marcador que el WorldModel usa para registrar la observación como hecho.
            "test_run": True,
        }
        if result.timed_out:
            payload["error"] = f"la suite excedió el timeout de {plan['timeout']}s y fue detenida"
        elif result.returncode not in (0, 1, 5):
            payload["error"] = (
                f"pytest terminó con exit code {result.returncode}: no se pudo ejecutar la suite"
            )
        return payload


class _Rejected(Exception):
    """Argumentos que no pasan la validación. No se ejecuta nada."""

    def __init__(self, message: str, detail: dict | None = None):
        super().__init__(message)
        self.detail = detail or {}


def parse_pytest_counts(stdout: str) -> dict[str, int]:
    """Conteos del resumen de pytest. Si no se puede leer, se devuelve vacío.

    Nunca se inventa un 0: un conteo no parseado se marca con `counts_parsed=False` para
    que quien verifique lo sepa antes de tomar un 0 por un hecho.
    """
    counts: dict[str, int] = {}
    for number, label in _COUNT_PATTERN.findall(stdout or ""):
        key = label.rstrip("s") if label in ("errors", "xfailed", "xpassed") else label
        key = "error" if key in ("error", "errors") else key
        counts[key] = counts.get(key, 0) + int(number)
    return counts


def build_test_tools(workspace: str | Path, **over) -> list[Tool]:
    """Tool `execute.test` lista para registrar, siguiendo el patrón de filesystem."""
    tool = TestRunnerTool(workspace, **over)
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


__all__ = [
    "ALLOWED_KEYS",
    "ALLOWED_RUNNERS",
    "FORBIDDEN_KEYS",
    "SELECTOR_PATTERN",
    "TestRunnerTool",
    "build_test_tools",
    "parse_pytest_counts",
]
