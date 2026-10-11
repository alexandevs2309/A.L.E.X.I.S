"""Herramientas git — leer, confirmar y publicar código, con controles de seguridad.

**Por qué existe un módulo entero para git, y no tres llamadas más en `terminal`.**
Porque `terminal` ejecuta lo que le pidan y git no es lo mismo: publica en un remoto,
escribe en el directorio `.git` y es el camino por el que un agente sale del workspace.
Las tres capacidades que expone el catálogo tienen contratos distintos y cada una necesita
su propia cortina.

**La defensa real no es la whitelist, es que no hay shell.** `SandboxRunner.run()` usa
`create_subprocess_exec(*argv)`: una lista de argumentos, nunca una cadena. En ese modelo
`;`, `&&` y `|` son texto inerte — no pueden ejecutar nada, se quedan como
argumento o rompen el parseo**. La whitelist de verbos es la segunda capa: impide que a
`git.read` le pidan `commit`, que quizá sea válido pero no es lectura.

**`git.push` sale a la red, y el sandbox no la aísla.** El propio `SandboxRunner` lo dice
en su docstring: el bloqueo de red exige namespaces con privilegios. Publicar abre un
socket, así que este módulo resuelve el remoto y valida la dirección con
`alexis.tools.net`, igual que cualquier otra tool de red del proyecto. Sin eso, un `origin`
apuntando a `https://otro-host/x` se lleva el token que el credential helper tenga
preparado — exfiltración de credenciales que ningún límite del sandbox vería.

**No se manejan credenciales.** Ni se leen, ni se almacenan, ni se registran. El entorno
del sandbox es fijo y sin secretos (`PATH`, `HOME`, `LANG`); quien autentica es git, con
la configuración que ya tenga el repositorio.
"""

from __future__ import annotations

import logging
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from alexis.security.sandbox import SandboxError, SandboxRunner
from alexis.tools.registry import Tool

log = logging.getLogger(__name__)

#: Verbos que `git.read` puede ejecutar. Es una lista de LECTURA por diseño: ninguno
#: escribe en el repositorio, ninguno habla con la red y ninguno puede dejar trabajo a
#: medias si se interrumpe. Añadir aquí un verbo exige justificar el cambio.
VERBOS_LECTURA: frozenset[str] = frozenset({"status", "log", "diff", "show", "ls-files"})

#: Caracteres que delatan un intento de encadenar comandos. No hacen falta para la
#: seguridad —no hay shell que los interprete— pero se rechazan igual y se explica por qué
#: en el mensaje: son la señal más clara de que alguien está probando el límite, y conviene
#: que quede registrado en la traza en vez de ignorarse en silencio.
_METACARACTERES = (";", "&&", "||", "|", "$(", "`", ">", "<", "\n")

#: Refs de git. No es una expresión regular completa: es una lista blanca de formas, y lo
#: que no encaja se rechaza. Se admite `origin/main` y `v1.2.3`, no `main;rm -rf`.
_RE_REF = re.compile(r"^[A-Za-z0-9._\-/]+$")

#: Nombre de remoto. Sin dos puntos, sin barra, sin arroba: `git` escribiría esto en
#: `.git/config` si se usa `--add`, así que se limita antes de llegar ahí.
_RE_REMOTO = re.compile(r"^[A-Za-z0-9._\-]+$")


class GitCommandRejected(ValueError):
    """El comando no pasa la validación previa. Nunca llega al proceso."""

    def __init__(self, motivo: str):
        super().__init__(motivo)
        self.motivo = motivo


def _rechazar_si_hay_metacaracteres(texto: str) -> None:
    for c in _METACARACTERES:
        if c in texto:
            raise GitCommandRejected(
                f"carácter no permitido {c!r}: git se invoca con lista de argumentos y "
                "sin shell, así que un metacarácter no puede ejecutarse; se rechaza para "
                "que el intento quede registrado"
            )


def parsear_lectura(comando: str) -> list[str]:
    """`(comando) -> argv` validado. Lanza `GitCommandRejected` si no es una lectura.

    Se parsea con `shlex` para entender comillas (`log -n 5`, `diff HEAD`) pero el
    resultado es una **lista**: lo que sale de aquí va tal cual a `create_subprocess_exec`.
    """
    bruto = (comando or "").strip()
    if not bruto:
        raise GitCommandRejected("comando vacío")
    _rechazar_si_hay_metacaracteres(bruto)
    try:
        argv = shlex.split(bruto)
    except ValueError as exc:
        raise GitCommandRejected(f"no se puede parsear: {exc}") from exc
    if not argv:
        raise GitCommandRejected("comando vacío tras parsear")

    verbo = argv[0]
    if verbo.startswith("-"):
        raise GitCommandRejected(f"el comando no puede empezar por '-': {verbo!r}")
    if verbo not in VERBOS_LECTURA:
        raise GitCommandRejected(
            f"verbo {verbo!r} no permitido; los de lectura son: "
            + ", ".join(sorted(VERBOS_LECTURA))
        )
    return argv


def parsear_ref(valor: str, campo: str) -> str:
    """Valida un ref o un nombre de remoto contra su patrón."""
    limpio = (valor or "").strip()
    if not _RE_REF.fullmatch(limpio):
        raise GitCommandRejected(f"{campo} con formato no permitido: {valor!r}")
    return limpio


def parsear_remoto(valor: str) -> str:
    limpio = (valor or "").strip() or "origin"
    if not _RE_REMOTO.fullmatch(limpio):
        raise GitCommandRejected(f"remoto con formato no permitido: {valor!r}")
    return limpio


def _host_de_url(url: str) -> str | None:
    """Extrae el host de una URL de remoto, sea `https://`, `ssh://` o `scp-like`."""
    texto = (url or "").strip()
    if "://" in texto:
        from urllib.parse import urlparse

        try:
            return urlparse(texto).hostname
        except ValueError:
            return None
    # Forma `git@github.com:org/repo.git`, que no es una URL sino notación scp.
    if "@" in texto and ":" in texto.split("@", 1)[1]:
        return texto.split("@", 1)[1].split(":", 1)[0]
    return None


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #


@dataclass
class GitReadTool:
    """`git.read` — consultas de solo lectura al repositorio.

    `risk=low`: lee, no escribe y no sale a la red.
    """

    sandbox: SandboxRunner
    name: str = "git.read"
    description: str = (
        "Consulta el repositorio git del workspace. Verbos admitidos: "
        + ", ".join(sorted(VERBOS_LECTURA))
        + " (ej. 'status', 'log -n 5', 'diff HEAD'). Sólo lectura."
    )
    risk: str = "low"
    schema: dict = field(default_factory=dict)
    permissions: dict = field(default_factory=dict)
    timeout: float = 10.0
    limits: dict = field(default_factory=dict)
    capability_id: str = "git.read"
    sandbox_profile: str = "sandbox-project"

    def __post_init__(self):
        self.handler = self._run

    async def _run(self, args: dict) -> dict:
        try:
            argv = parsear_lectura(str(args.get("command", "")))
        except GitCommandRejected as exc:
            # Antes del sandbox: este comando nunca va a ejecutarse, y rejection aquí
            # significa que el agente pidió algo fuera del contrato de lectura.
            return {"ok": False, "error": str(exc.motivo), "rejected": True}
        result = await self.sandbox.run(["git", *argv])
        return {"ok": result.returncode == 0, "command": " ".join(argv),
                "output": result.stdout, "error": result.stderr or None}


@dataclass
class GitCommitTool:
    """`git.commit` — confirma cambios dentro del workspace.

    `risk=high`: escribe en el historial, que es difícil de revertir a ojo. El catálogo lo
    declara `high` y esta tool lo respeta; el enunciado original pedía `medium` y aquí se
    sigue al catálogo, que es el más restrictivo de los dos.
    """

    sandbox: SandboxRunner
    name: str = "git.commit"
    description: str = (
        "Confirma cambios del workspace con un mensaje. Los ficheros deben estar dentro "
        "del workspace; los que estén fuera se rechazan."
    )
    risk: str = "high"
    schema: dict = field(default_factory=dict)
    permissions: dict = field(default_factory=dict)
    timeout: float = 20.0
    limits: dict = field(default_factory=dict)
    capability_id: str = "git.commit"
    sandbox_profile: str = "sandbox-project"
    user_repository: Any = None

    def __post_init__(self):
        self.handler = self._run

    async def _run(self, args: dict) -> dict:
        mensaje = str(args.get("message", "")).strip()
        ficheros = args.get("files") or []
        if not mensaje:
            return {"ok": False, "error": "falta 'message'"}
        if not isinstance(ficheros, list) or not ficheros:
            return {"ok": False, "error": "falta 'files'"}

        # Todo fichero pasa por el sandbox ANTES de tocar git. `resolve_in_workspace`
        # resuelve y comprueba el containment; un `../../etc/passwd` cae aquí, y no hay
        # ningún punto después donde un camino absoluto o relativo pudiese colarse.
        try:
            resueltos = [self.sandbox.resolve_in_workspace(str(f)) for f in ficheros]
        except SandboxError as exc:
            return {"ok": False, "error": str(exc), "rejected": True}

        relativos = [str(p.relative_to(Path(self.sandbox.workspace).resolve()))
                     for p in resueltos]

        avisos: list[str] = []
        # La preferencia es un AVISO, no un veto: el usuario pidió commits atómicos, no que
        # ALEXIS decida por su cuenta qué se confirma.
        if self.user_repository is not None:
            try:
                pref = await self.user_repository.get("style", "commit_granularity")
            except Exception:  # noqa: BLE001 — una preferencia ausente no puede romper un commit
                pref = None
            if pref is not None and pref.value == "atomic" and len(relativos) > 5:
                avisos.append(
                    f"el usuario prefiere commits atómicos y esto confirma {len(relativos)} "
                    "ficheros en uno solo"
                )

        add = await self.sandbox.run(["git", "add", "--", *relativos])
        if add.returncode != 0:
            return {"ok": False, "error": f"git add falló: {add.stderr}"}

        commit = await self.sandbox.run(["git", "commit", "-m", mensaje, "--", *relativos])
        if commit.returncode != 0:
            return {"ok": False, "error": f"git commit falló: {commit.stderr}"}

        return {"ok": True, "files": relativos, "message": mensaje,
                "output": commit.stdout, "warnings": avisos}


@dataclass
class GitPushTool:
    """`git.push` — publica en un remoto. Sale a la red y por eso valida el destino.

    `risk=critical`: publica código fuera de esta máquina. La policy exige aprobación
    explícita siempre, y el remoto se resuelve y se comprueba antes de abrir el socket.
    """

    sandbox: SandboxRunner
    name: str = "git.push"
    description: str = (
        "Publica la rama actual en un remoto git. Es una acción irreversible hacia fuera: "
        "requiere aprobación y el destino se valida antes de conectar."
    )
    risk: str = "critical"
    schema: dict = field(default_factory=dict)
    permissions: dict = field(default_factory=dict)
    timeout: float = 60.0
    limits: dict = field(default_factory=dict)
    capability_id: str = "git.push"
    sandbox_profile: str = "network-observed"
    user_repository: Any = None

    def __post_init__(self):
        self.handler = self._run

    async def _resolve_and_check_remote(self, remoto: str) -> tuple[str, str]:
        """`(host, url)` del remoto, comprobando que el host sea público.

        Por qué esta comprobación es obligatoria: el sandbox NO aísla la red (lo dice su propio
        docstring), así que aquí es la única barrera. Sin ella, un `origin` que apunte a
        `https://otro-host/x` recibe lo que el credential helper de git tenga listo, y
        eso es robo de credenciales que ninguna otra capa del runtime vería.

        La comprobación usa `net.resolve_public()`, la MISMA función que protege a
        `browser.research`. Que sea la misma y no una copia es lo que evita que las dos se
        separen con el tiempo.
        """
        from alexis.tools import net as net_mod

        got = await self.sandbox.run(["git", "remote", "get-url", remoto])
        if got.returncode != 0:
            raise GitCommandRejected(
                f"no se pudo resolver el remoto {remoto!r}: {got.stderr.strip() or 'desconocido'}"
            )
        url = got.stdout.strip()
        host = _host_de_url(url)
        if not host:
            raise GitCommandRejected(f"URL de remoto ininterpretable: {url!r}")
        publicas, motivo = net_mod.resolve_public(host)
        if not publicas:
            raise GitCommandRejected(
                f"el remoto {remoto!r} apunta a {host}, que no es una dirección pública "
                f"({motivo}). No se publica a un destino no verificable."
            )
        return host, url

    async def _run(self, args: dict) -> dict:
        remoto = parsear_remoto(str(args.get("remote", "origin")))
        rama = parsear_ref(str(args.get("branch", "")), "branch")
        if not rama:
            return {"ok": False, "error": "falta 'branch'"}

        try:
            host, url = await self._resolve_and_check_remote(remoto)
        except GitCommandRejected as exc:
            return {"ok": False, "error": str(exc.motivo), "rejected": True}

        # La preferencia `auto_approve_git_push` NO se consulta aquí a propósito. Esta tool
        # se ejecuta cuando el gate y la policy ya han autorizado; lo que hace la
        # preferencia es evitar que la CONFIRMACIÓN se repita en cada llamada, y eso lo
        # decide la capa de aprobación, no la tool. Saltarse la comprobación del remoto
        # porque el usuario lo pidió alguna vez sería confiar en una preferencia
        # persistente para autorizar exfiltración de credenciales.
        push = await self.sandbox.run(
            ["git", "push", remoto, rama], env={"GIT_TERMINAL_PROMPT": "0"}
        )
        return {"ok": push.returncode == 0, "remote": remoto, "branch": rama,
                "host": host, "url": url, "output": push.stdout,
                "error": push.stderr or None}


# --------------------------------------------------------------------------- #
# Esquemas
# --------------------------------------------------------------------------- #

_SCHEMA_READ = {
    "type": "object",
    "properties": {
        "command": {
            "type": "string",
            "description": "Comando git de sólo lectura. Verbos: "
                           + ", ".join(sorted(VERBOS_LECTURA)),
        }
    },
    "required": ["command"],
}

_SCHEMA_COMMIT = {
    "type": "object",
    "properties": {
        "message": {"type": "string", "description": "Mensaje del commit"},
        "files": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Ficheros a confirmar, todos dentro del workspace",
        },
    },
    "required": ["message", "files"],
}

_SCHEMA_PUSH = {
    "type": "object",
    "properties": {
        "remote": {"type": "string", "description": "Nombre del remoto (por defecto 'origin')"},
        "branch": {"type": "string", "description": "Rama a publicar"},
    },
    "required": ["branch"],
}


def build_git_tools(
    workspace: str | Path, *, user_repository: Any = None
) -> list[Tool]:
    """Construye las tres tools git sobre un sandbox acotado al workspace."""
    sandbox = SandboxRunner(workspace)
    read = GitReadTool(sandbox=sandbox, schema=_SCHEMA_READ,
                       permissions={"read": True, "network": False})
    commit = GitCommitTool(sandbox=sandbox, schema=_SCHEMA_COMMIT,
                           permissions={"write": True, "network": False},
                           user_repository=user_repository)
    push = GitPushTool(sandbox=sandbox, schema=_SCHEMA_PUSH,
                       permissions={"write": True, "network": True},
                       user_repository=user_repository)
    return [
        Tool(name=t.name, description=t.description, risk=t.risk, handler=t.handler,
             schema=t.schema, permissions=t.permissions, timeout=t.timeout,
             limits=t.limits, capability_id=t.capability_id,
             sandbox_profile=t.sandbox_profile)
        for t in (read, commit, push)
    ]


__all__ = [
    "GitCommandRejected",
    "GitCommitTool",
    "GitPushTool",
    "GitReadTool",
    "VERBOS_LECTURA",
    "build_git_tools",
    "parsear_lectura",
    "parsear_ref",
    "parsear_remoto",
]