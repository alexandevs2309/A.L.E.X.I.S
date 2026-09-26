#!/usr/bin/env python3
"""R1 — detector de credenciales en texto plano (pre-commit / Makefile).

Falla (exit 1) si encuentra un archivo versionable que contenga algo con aspecto de
credencial real: `sk_...`, tokens de GitHub, claves AWS, JWT, o un valor largo
asignado a una variable llamada *key/token/secret/password*.

Reglas de diseño:
- **Nunca imprime el valor encontrado**: sólo ruta, línea y qué patrón casó. Un
  detector que filtra el secreto en su propio log es peor que no tener detector.
- Ignora `*.example` (plantillas por definición sin valor) y `.env.example`.
- Ignora directorios de build/venv/cachés.

Uso:
    python scripts/check_secrets.py [ruta ...]     # por defecto: raíz del repo
"""

import re
import sys
from pathlib import Path

#: Patrones de credenciales conocidas. Se reporta el nombre, nunca el valor.
SIGNATURE_PATTERNS: dict[str, re.Pattern] = {
    "openai-style key (sk_...)": re.compile(r"\bsk-[A-Za-z0-9_-]{16,}|\bsk_[A-Za-z0-9]{20,}"),
    "anthropic key (sk-ant-...)": re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}"),
    "github token (ghp_/gho_/ghs_/ghu_)": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    "github fine-grained token": re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    "aws access key (AKIA...)": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}"),
    "google api key (AIza...)": re.compile(r"\bAIza[A-Za-z0-9_-]{30,}"),
    "slack token (xox*)": re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"),
    "jwt (eyJ...)": re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    "private key block": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----"),
    # La contraseña se valida aparte (DSN_PATTERN) para descartar `change-me`.
    "connection string con password": re.compile(
        r"\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis)://[^\s:@/]+:[^\s:@/]+@"
    ),
}

#: `KEY=valor` donde el nombre de la variable delata una credencial.
ASSIGNMENT_PATTERN = re.compile(
    r"(?im)^\s*(?:export\s+)?([A-Za-z0-9_.-]*(?:api[_-]?key|secret|token|password|passwd|pwd)[A-Za-z0-9_.-]*)"
    r"\s*[:=]\s*[\"']?([^\s\"'#]{16,})"
)

#: Un valor sólo es sospechoso si parece un LITERAL de credencial. Sin esto, cualquier
#: expresión de código como `API_TOKEN = require_api_token()` sería un falso positivo.
LITERAL_VALUE_PATTERN = re.compile(r"^[A-Za-z0-9_\-.+/=]+$")

#: Nombres que contienen "token" pero NO son credenciales (conteos, presupuestos).
#: Sin esta lista, `cost_per_1k_tokens=...` sería un falso positivo permanente.
NON_SECRET_NAME_PATTERN = re.compile(
    r"tokens$|tokens\b|_tokens?\b|token_(?:budget|count|estimate|usage|limit|window)"
)

#: Partes de una DSN cuyo usuario/contraseña son marcadores de posición.
DSN_PLACEHOLDERS = ("change-me", "changeme", "password", "secret", "user", "admin", "localhost", "127.0.0.1")

#: Patrón de DSN con credenciales embebidas, con la contraseña capturada para poder
#: descartar marcadores de posición (`postgresql://alexis:change-me@host`).
DSN_PATTERN = re.compile(
    r"\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis)://[^\s:@/]+:([^\s:@/]+)@"
)

#: Valores que son claramente placeholders: no deben considerarse filtraciones.
PLACEHOLDER_MARKERS = (
    "change-me", "changeme", "your-", "your_", "replace_me", "reemplazar", "placeholder",
    "example", "dummy", "fake", "sample", "xxxx", "todo", "none", "null", "<", ">", "${",
    "env:", "localhost", "127.0.0.1", "test-token", "dev-token",
)

# `secrets/` NO está en SKIP_DIRS a propósito: sus ficheros reales se escanean aunque git
# los ignore, porque una credencial en claro en disco es un riesgo exista o no el repo.
# Eso hace que el check falle en la máquina que tiene secretos reales; ver
# docs/DEVELOPMENT.md §"El check de secretos y los secretos reales".

SKIP_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", "dist",
    "build", ".cache", ".mypy_cache", ".ruff_cache", "pgdata",
}

SKIP_SUFFIXES = {".example", ".pyc", ".pyo", ".png", ".jpg", ".jpeg", ".gif", ".glb", ".bin"}


def _is_placeholder(value: str) -> bool:
    lowered = value.lower()
    return any(marker in lowered for marker in PLACEHOLDER_MARKERS)


def _looks_like_placeholder_token(value: str) -> bool:
    """Un valor con few tokens distintos y mucho carácter de relleno no es una key real."""
    unique = len(set(value))
    if unique <= 4 and len(value) >= 16:
        return True
    if value.count("-") + value.count("_") >= max(4, len(value) // 4):
        return True
    return False


def scan_text(text: str) -> list[tuple[int, str]]:
    """Devuelve `[(línea, patrón)]` para un texto. No devuelve valores."""
    findings: list[tuple[int, str]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for name, pattern in SIGNATURE_PATTERNS.items():
            if name == "connection string con password":
                match = DSN_PATTERN.search(line)
                if match and not _is_placeholder(match.group(1)):
                    findings.append((lineno, name))
                continue
            if pattern.search(line):
                findings.append((lineno, name))
        for match in ASSIGNMENT_PATTERN.finditer(line):
            name = match.group(1)
            value = match.group(2).strip().strip("\"'")
            if NON_SECRET_NAME_PATTERN.search(name):
                continue
            if not LITERAL_VALUE_PATTERN.match(value):
                continue  # es una expresión de código, no un literal
            if _is_placeholder(value) or _looks_like_placeholder_token(value):
                continue
            findings.append((lineno, f"valor de {name}"))
    return findings


def iter_files(roots: list[Path]) -> list[Path]:
    files: list[Path] = []
    for root in roots:
        if root.is_file():
            files.append(root)
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            if path.suffix in SKIP_SUFFIXES or path.name.endswith(".example"):
                continue
            files.append(path)
    return files


def main(argv: list[str]) -> int:
    repo_root = Path(__file__).resolve().parent.parent
    roots = [Path(a).resolve() for a in argv[1:]] or [repo_root]
    if not roots:
        print("check_secrets: sin rutas que revisar", file=sys.stderr)
        return 0

    findings: list[tuple[Path, int, str]] = []
    scanned = 0
    for path in iter_files(roots):
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        scanned += 1
        for lineno, pattern in scan_text(text):
            findings.append((path, lineno, pattern))

    if findings:
        print("check_secrets: FALLA — posibles credenciales en texto plano", file=sys.stderr)
        for path, lineno, pattern in findings:
            try:
                shown = path.relative_to(repo_root)
            except ValueError:
                shown = path
            print(f"  {shown}:{lineno}: {pattern}", file=sys.stderr)
        print(
            "\nNo se imprime el valor detectado. Mueve el secreto a secrets/ (ignorado por"
            " git) o a un gestor de secretos, rota la credencial y vuelve a lanzar el check.",
            file=sys.stderr,
        )
        return 1

    print(f"check_secrets: OK — {scanned} archivos revisados, 0 credenciales detectadas")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
