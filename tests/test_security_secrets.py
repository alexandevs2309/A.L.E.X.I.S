"""Tests R1 — el detector de secretos falla ante credenciales reales y no ante plantillas.

No se toca ningún secreto real: todos los valores de prueba son sintéticos y se escriben
en `tmp_path`.
"""

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_secrets.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("check_secrets", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check_secrets = _load_module()


def lit(*parts: str) -> str:
    """Compone un valor en runtime.

    Los fixtures de este test NECESITAN ser credenciales con aspecto real para probar
    que el detector las caza, pero el fichero fuente no debe contener literales que el
    propio detector marque (sería auto-sabotaje). Por eso se componen por trozos: en
    disco no hay patrón, en memoria sí.
    """
    return "".join(parts)


# ----------------------------------------------------------------------
# Detecta credenciales reales
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        lit("ALEXIS_ELEVENLABS_API_KEY=sk_", "0123456789abcdefghijklmnopqrstuvwxyz"),
        lit("OPENAI_API_KEY=sk-proj-", "0123456789abcdefghijklmnop"),
        lit("ANTHROPIC_API_KEY=sk-ant-", "0123456789abcdefghijklmnop"),
        lit("GITHUB_TOKEN=gh", "p_0123456789abcdefghijklmnopqrstuvwxyz"),
        lit("TOKEN=github_pat_", "0123456789abcdefghijklmnopqrst"),
        lit("AWS_ACCESS_KEY_ID=AKIA", "IOSFODNN7EXAMPLE"),
        lit("GOOGLE_KEY=AIza", "SyA0123456789abcdefghijklmnopqrstuv"),
        lit("SLACK=xox", "b-0123456789-0123456789abcdef"),
        lit(
            "JWT=eyJhbGciOiJIUzI1NiJ9.",
            "eyJzdWIiOiIxMjM0NSJ9.",
            "dBjftJeZ4CVPmB92K27uhbUJU1p1r",
        ),
        lit("-----BEGIN ", "RSA PRIVATE KEY-----"),
        lit("DATABASE_URL=postgresql://alexis:", "Sup3rS3cretReal@db:5432/alexis"),
        lit("DB_PASSWORD=", "9fj2Kd0sLkz8Qm2x7Pv4Rt1"),
    ],
)
def test_detects_real_looking_credentials(line):
    assert check_secrets.scan_text(line), f"debería detectar: {line[:30]}…"


# ----------------------------------------------------------------------
# No marca plantillas ni valores de configuración inocuos
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        "ELEVENLABS_API_KEY=",
        "POSTGRES_PASSWORD=change-me",
        "ALEXIS_API_TOKEN=REPLACE_ME",
        "SECRET_KEY=your-secret-here",
        "TOKEN=xxxx-xxxx-xxxx-xxxx-xxxx-xxxx",
        "PASSWORD=<pon-aqui-tu-password>",
        "API_KEY=${ALEXIS_ELEVENLABS_API_KEY}",
        'PASSWORD="placeholder"',
        "postgresql://alexis:change-me@localhost:5433/alexis",
        "cost_per_1k_tokens=0.0",
        "max_tokens=1500",
        "token_budget=4000",
    ],
)
def test_ignores_placeholders_and_non_secrets(line):
    assert not check_secrets.scan_text(line), f"falso positivo: {line}"


# ----------------------------------------------------------------------
# Nunca imprime el valor
# ----------------------------------------------------------------------


def test_findings_never_expose_the_value(tmp_path, capsys):
    secret = lit("sk_", "0123456789abcdefghijklmnopqrstuvwxyz")
    target = tmp_path / "leaked.env"
    target.write_text(f"ALEXIS_ELEVENLABS_API_KEY={secret}\n", encoding="utf-8")
    code = check_secrets.main(["check_secrets", str(target)])
    assert code == 1  # falla, como debe
    err = capsys.readouterr().err
    assert secret not in err  # el valor jamás aparece en el log
    assert "leaked.env" in err  # pero sí la ruta y el patrón


# ----------------------------------------------------------------------
# Recorrido de archivos
# ----------------------------------------------------------------------


def test_example_files_are_skipped(tmp_path):
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "elevenlabs.env.example").write_text(
        "ALEXIS_ELEVENLABS_API_KEY=\n# pon aquí tu key sk_...\n", encoding="utf-8"
    )
    scanned = check_secrets.iter_files([tmp_path])
    assert scanned == []


def test_real_env_file_is_scanned(tmp_path):
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    real = secrets_dir / "elevenlabs.env"
    real.write_text(
        "ALEXIS_ELEVENLABS_API_KEY=" + lit("sk_", "0123456789abcdefghijklmnop") + "\n",
        encoding="utf-8",
    )
    scanned = check_secrets.iter_files([tmp_path])
    assert real in scanned


def test_repo_examples_are_clean():
    """Las plantillas del repo no pueden contener credenciales."""
    root = Path(__file__).resolve().parent.parent
    for example in (root / "secrets").glob("*.example"):
        assert not check_secrets.scan_text(example.read_text(encoding="utf-8")), example


def test_gitignore_allows_examples_but_not_secrets():
    root = Path(__file__).resolve().parent.parent
    rules = (root / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "secrets/*" in rules
    assert "!secrets/*.example" in rules
