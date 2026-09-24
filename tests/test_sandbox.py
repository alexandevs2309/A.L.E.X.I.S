import asyncio
import pathlib
import sys
import tempfile

import pytest

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from alexis.security.sandbox import SandboxError, SandboxRunner


def _runner(timeout=2.0):
    ws = pathlib.Path(tempfile.mkdtemp())
    (ws / "hola.txt").write_text("contenido\n", encoding="utf-8")
    return SandboxRunner(ws, timeout=timeout), ws


@pytest.mark.asyncio
async def test_sandbox_runs_command():
    runner, _ = _runner()
    result = await runner.run([sys.executable, "-c", "print('hola sandbox')"])
    assert result.ok is True
    assert result.returncode == 0
    assert "hola sandbox" in result.stdout


@pytest.mark.asyncio
async def test_sandbox_rejects_outside_workspace():
    runner, _ = _runner()
    with pytest.raises(SandboxError):
        runner.resolve_in_workspace("/etc/passwd")
    with pytest.raises(SandboxError):
        runner.resolve_in_workspace("../secreto.txt")


@pytest.mark.asyncio
async def test_sandbox_timeout_kills():
    runner, _ = _runner()
    result = await runner.run([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.4)
    assert result.timed_out is True


@pytest.mark.asyncio
async def test_sandbox_rlimit_memory():
    runner, _ = _runner()
    result = await runner.run(
        [sys.executable, "-c", "x = []; [x.append(b'a' * 1_000_000) for _ in range(6000)]"],
        timeout=3.0,
    )
    assert result.ok is False
    assert "memory" in result.stderr.lower() or result.returncode != 0