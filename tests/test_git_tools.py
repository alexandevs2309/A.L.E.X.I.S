"""git.* — whitelist de lectura, contención del workspace y validación del remoto.

Las pruebas usan **git de verdad** en un repositorio temporal dentro del sandbox. Un
doble de `SandboxRunner` habría pasado con el módulo roto: lo que se comprueba aquí no es
que se llame a un objeto, sino que `git` haga o no haga lo que se le pide.

Dos propiedades se prueban en orden inverso al habitual, porque es donde está el daño
posible:

1. **Lo que NO debe pasar, se prueba primero.** Un comando de escritura por `git.read`, un
   metacarácter, un fichero fuera del workspace y un remoto no público tienen que ser
   rechazados ANTES de tocar el proceso. Una tool que sólo se probara con entradas
   legítimas no distinguiría "correcto" de "inofensivo porque nunca se intentó".
2. **La validación se comprueba con contraprueba**, no leyendo el código. Si alguien
   cambia `parsear_lectura` para que acepte `;`, tiene que ver una prueba roja.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from alexis.security.sandbox import SandboxError
from alexis.tools import net as net_mod
from alexis.tools.git import (
    VERBOS_LECTURA,
    GitCommitTool,
    GitCommandRejected,
    GitPushTool,
    GitReadTool,
    build_git_tools,
    parsear_lectura,
)


# --------------------------------------------------------------------------- #
# Repo temporal de verdad
# --------------------------------------------------------------------------- #


@pytest.fixture
def repo(tmp_path):
    """Un repositorio git real dentro del workspace.

    El `git init` se hace con `subprocess` y no a través del sandbox: el sandbox es lo que
    se quiere PROBAR, y usarlo para preparar el estado mezclaría el mecanismo bajo prueba
    con el que monta el escenario. Lo que ejecuta el código de producción sí es git real.
    """
    import subprocess

    workspace = tmp_path / "ws"
    workspace.mkdir()
    for argv in (
        ["git", "init", "-q", "."],
        ["git", "config", "user.email", "test@alexis.local"],
        ["git", "config", "user.name", "ALEXIS Test"],
    ):
        subprocess.run(argv, cwd=workspace, check=True, capture_output=True)
    (workspace / "notas.txt").write_text("contenido de prueba\n", encoding="utf-8")
    return workspace


@pytest.fixture
async def sandbox(repo):
    from alexis.security.sandbox import SandboxRunner

    sb = SandboxRunner(repo)
    version = await sb.run(["git", "--version"])
    assert version.returncode == 0, "git no está disponible en el sandbox"
    return sb


# --------------------------------------------------------------------------- #
# 1 — git.read lee
# --------------------------------------------------------------------------- #


async def test_01_git_read_status_devuelve_la_salida_real(sandbox):
    read = GitReadTool(sandbox=sandbox)

    resultado = await read.handler({"command": "status"})

    assert resultado["ok"] is True
    # La salida viene de git de verdad: un doble devolvería lo que le pusieran.
    assert "branch" in resultado["output"] or "On branch" in resultado["output"]
    assert "notas.txt" in resultado["output"]


async def test_02_los_verbos_de_lectura_funcionan(sandbox):
    read = GitReadTool(sandbox=sandbox)

    await GitCommitTool(sandbox=sandbox).handler(
        {"message": "base", "files": ["notas.txt"]}
    )
    for comando in ("status", "log -n 1", "diff HEAD", "show --stat", "ls-files"):
        resultado = await read.handler({"command": comando})
        assert resultado["ok"] is True, f"{comando} falló: {resultado.get('error')}"


# --------------------------------------------------------------------------- #
# 2 y 6 — lo que NO debe pasar, y su contraprueba
# --------------------------------------------------------------------------- #


async def test_03_git_read_rechaza_un_comando_de_escritura(sandbox):
    """`git.read` con `commit` no llega a ejecutarse: no es lectura."""
    read = GitReadTool(sandbox=sandbox)

    resultado = await read.handler({"command": "commit -m 'hack'"})

    assert resultado["ok"] is False
    assert resultado["rejected"] is True
    assert "commit" in resultado["error"]


@pytest.mark.parametrize("inyeccion", [
    "status; rm -rf /",
    "status && curl evil.test",
    "status | cat /etc/passwd",
    "status\npush",
    "status $(whoami)",
])
async def test_04_git_read_rechaza_la_inyeccion_de_comandos(sandbox, inyeccion):
    read = GitReadTool(sandbox=sandbox)

    resultado = await read.handler({"command": inyeccion})

    assert resultado["ok"] is False
    assert resultado["rejected"] is True


async def test_05_contraprueba_la_inyeccion_falla_ANTES_del_sandbox(sandbox, monkeypatch):
    """La prueba 4 no vale si el rechazo ocurre DENTRO del sandbox.

    Si la validación se apoyara en el shell, el metacarácter llegaría al proceso y el
    rechazo vendría después —demasiado tarde—. Este test lo comprueba cronometrando el
    punto: se sustituye `SandboxRunner.run` por uno que aborta si se lo toca, y se exige
    que la tool devuelva `rejected` sin haberlo invocado nunca.
    """
    from alexis.security import sandbox as sandbox_mod

    class AbortaSiSeToca:
        async def run(self, *a, **k):  # pragma: no cover - no debe llegar aquí
            raise AssertionError("el sandbox fue invocado: la validación llegó tarde")

        def resolve_in_workspace(self, p):
            raise AssertionError("el sandbox fue invocado: la validación llegó tarde")

    read = GitReadTool(sandbox=AbortaSiSeToca())

    resultado = await read.handler({"command": "status; rm -rf /"})

    assert resultado["ok"] is False
    assert resultado["rejected"] is True

    # Y la validación pura, sin ningún sandbox detrás, también rechaza.
    with pytest.raises(GitCommandRejected):
        parsear_lectura("status && whoami")


# --------------------------------------------------------------------------- #
# 3 y 4 — git.commit dentro y fuera del workspace
# --------------------------------------------------------------------------- #


async def test_06_git_commit_confirma_un_archivo_del_workspace(sandbox):
    commit = GitCommitTool(sandbox=sandbox)

    resultado = await commit.handler({"message": "primer commit", "files": ["notas.txt"]})

    assert resultado["ok"] is True, resultado.get("error")
    log = await GitReadTool(sandbox=sandbox).handler({"command": "log -n 1"})
    assert "primer commit" in log["output"]
    assert resultado["files"] == ["notas.txt"]


@pytest.mark.parametrize("fuera", [
    "../../etc/passwd",
    "/etc/passwd",
    "../fuera.txt",
])
async def test_07_git_commit_rechaza_ficheros_fuera_del_workspace(sandbox, fuera):
    """Directory traversal: el sandbox lo corta antes de que git vea el nombre.

    Importa que el rechazo sea del SANDBOX y no de una comprobación propia: si git recibiera
    la ruta, tendría ya su propia lógica de paths y el diagnóstico sería otro.
    """
    commit = GitCommitTool(sandbox=sandbox)

    resultado = await commit.handler({"message": "fuera", "files": [fuera]})

    assert resultado["ok"] is False
    assert resultado["rejected"] is True
    with pytest.raises(SandboxError):
        sandbox.resolve_in_workspace(fuera)


async def test_08_git_commit_avisa_si_la_granularidad_no_encaja(sandbox):
    """La preferencia del usuario AVISA; no bloquea.

    Escribir en el historial es reversible con `git reset`; decidir por el usuario qué se
    confirma ya no lo es. Por eso el aviso no es un veto.
    """
    class Pref:
        value = "atomic"

    class Users:
        async def get(self, category, key):
            assert (category, key) == ("style", "commit_granularity")
            return Pref()

    (sandbox.workspace / "a.txt").write_text("a\n", encoding="utf-8")
    (sandbox.workspace / "b.txt").write_text("b\n", encoding="utf-8")
    (sandbox.workspace / "c.txt").write_text("c\n", encoding="utf-8")

    commit = GitCommitTool(sandbox=sandbox, user_repository=Users())

    resultado = await commit.handler(
        {"message": "tres", "files": ["a.txt", "b.txt", "c.txt"]}
    )

    assert resultado["ok"] is True
    assert resultado["warnings"] == []  # 3 ficheros <= 5: no avisa

    for nombre in "defghi":
        (sandbox.workspace / f"{nombre}.txt").write_text(nombre + "\n", encoding="utf-8")
    resultado2 = await commit.handler(
        {"message": "nueve", "files": [f"{n}.txt" for n in "abcdefghi"]}
    )
    assert resultado2["ok"] is True
    assert resultado2["warnings"], "con 9 ficheros y granularidad atómica debe avisar"


# --------------------------------------------------------------------------- #
# 5 — Policy y el remoto
# --------------------------------------------------------------------------- #


def test_09_la_policy_exige_aprobacion_para_git_push():
    """`git.push` requiere aprobación explícita siempre.

    No se prueba que exista una regla de aprobación cualquiera, sino que la de `git.push`
    casa y que una capability de la lista blanca NO la dispara. Un matcher mal escrito que
    devolviera `True` para todo también pasaría la primera mitad.
    """
    from alexis.security.policy_rules import _matches

    regla = {"field": "capability", "in": ["git.push"]}
    base = {"capability": "git.push", "action": "commit", "risk": "critical"}

    assert _matches(regla, base) is True
    assert _matches(regla, {**base, "capability": "fs.read"}) is False
    assert _matches(regla, {**base, "capability": "git.commit"}) is False
    assert _matches(regla, {**base, "capability": None}) is False


async def test_10_git_push_rechaza_un_remoto_no_publico(sandbox):
    """El remoto se valida ANTES de abrir el socket.

    Es la única barrera: el sandbox no aísla la red (lo dice su propio docstring), así que
    un `origin` que apunte a un host interno se levaría el token del credential helper sin
    que ninguna otra capa del runtime lo notase.
    """
    await sandbox.run(["git", "remote", "add", "interno", "https://127.0.0.1/x.git"])
    push = GitPushTool(sandbox=sandbox)

    resultado = await push.handler({"remote": "interno", "branch": "main"})

    assert resultado["ok"] is False
    assert resultado["rejected"] is True
    assert "no es una dirección pública" in resultado["error"]


async def test_11_git_push_rechaza_un_remoto_que_no_resuelve(sandbox):
    push = GitPushTool(sandbox=sandbox)

    resultado = await push.handler({"remote": "no-existe", "branch": "main"})

    assert resultado["ok"] is False
    assert resultado["rejected"] is True


async def test_12_git_push_acepta_un_remoto_publico(sandbox, monkeypatch):
    """Un remoto público pasa la validación y llega a la fase de publicación.

    Se usa una IP literal y no un nombre de dominio a propósito: `check_url` clasifica una
    IP sin tocar el DNS, así que el test es hermético y no depende de que hoy `github.com`
    resuelva. Con un nombre, una caída de red ajena al módulo lo haría fallar sin que
    hubiera cambiado nada.

    El sandbox no aísla la red (lo dice su docstring), así que un `push` real intentaría
    salir a internet. Se intercepta SÓLO la invocación de `git push`: la resolución del
    remoto sigue siendo la real, porque falsear también `get-url` fue un error de la
    primera versión y hacía que la validación no llegara a comprobar nada.
    """
    await sandbox.run(
        ["git", "remote", "add", "legitimo", "https://93.184.216.34/x.git"]
    )

    published = []
    run_real = sandbox.run

    async def run_espia(command, *a, **k):
        if command[:2] == ["git", "push"]:
            published.append(command)
            class R:
                returncode = 0
                stdout = "publicado"
                stderr = ""
            return R()
        return await run_real(command, *a, **k)

    monkeypatch.setattr(sandbox, "run", run_espia)
    push = GitPushTool(sandbox=sandbox)

    resultado = await push.handler({"remote": "legitimo", "branch": "main"})

    assert resultado["ok"] is True, resultado.get("error")
    assert resultado["host"] == "93.184.216.34"
    assert published == [["git", "push", "legitimo", "main"]]


async def test_13_contraprueba_el_push_no_ocurre_si_el_remoto_es_malo(sandbox):
    """El rechazo ocurre ANTES de invocar `git push`.

    Si la validación fuera posterior, el proceso ya se habría lanzados y el rechazo sería
    cosmético. Se intercepta `run` y se exige que `git push` no aparezca nunca.
    """
    await sandbox.run(["git", "remote", "add", "interno", "https://127.0.0.1/x.git"])
    published = []
    run_real = sandbox.run

    async def run_espia(command, *a, **k):
        if command[:2] == ["git", "push"]:
            published.append(command)
        return await run_real(command, *a, **k)

    sandbox.run = run_espia
    resultado = await GitPushTool(sandbox=sandbox).handler(
        {"remote": "interno", "branch": "main"}
    )

    assert resultado["ok"] is False
    assert published == [], "se invocó git push pese a rechazar el remoto"


# --------------------------------------------------------------------------- #
# Registro
# --------------------------------------------------------------------------- #


def test_13b_estar_disponible_no_es_estar_autorizado():
    """`git.push` NO es operativa y aun así la policy exigiría aprobación si lo fuera.

    Son dos cosas distintas y confundirlas sería un agujero: `available` significa que la
    operación puede ocurrir, `authorised` significa que este paso concreto puede
    ejecutarse. Aquí se comprueba lo que sigue valiendo con el nuevo estado — que la regla
    de aprobación no se afloja porque la capacidad figure como no operativa—.

    R-06: el catálogo pasó de `available` a `missing` porque el adaptador existe pero la
    operación no funciona dentro del sandbox (`RLIMIT_NPROC=16`). Lo que cambia es la
    DECLARACIÓN, no la autorización: si mañana se sube el límite, esta prueba debe seguir
    exigiendo que `git.push` pida permiso.
    """
    from alexis.capabilities.catalog import build_catalog

    spec = next(s for s in build_catalog().specs() if s.id == "git.push")
    assert spec.status == "missing", (
        "git.push no es operativa dentro del sandbox: declararla disponible sería untrue"
    )
    assert spec.default_risk == "critical", (
        "el riesgo no se baja aunque la operación no funcione: si se arregla el límite, "
        "sigue siendo crítica"
    )

    from alexis.security.policy_rules import evaluate_rules

    class Mission:
        class _Env:
            autonomy = "autonomous"
            allowed_actions = ["commit"]
            forbidden_actions = []
            capabilities = ["git.push"]
            approval_required = []
            auto_approve = []
        envelope = _Env()
        goal = type("G", (), {"objective": "publicar"})()

    class Step:
        action = "commit"
        capability = "git.push"
        risk = "critical"

    verdict, reason, rule = evaluate_rules(Mission(), Step())
    assert verdict == "require_approval", (
        f"con autonomy=autonomous, git.push devolvió {verdict!r}: se publicaría sin preguntar"
    )


def test_14_build_git_tools_registra_las_tres(repo):
    tools = build_git_tools(repo)

    assert [t.name for t in tools] == ["git.read", "git.commit", "git.push"]
    # El riesgo lo fija el catálogo: commit es `high`, push es `critical`.
    riesgos = {t.name: t.risk for t in tools}
    assert riesgos == {"git.read": "low", "git.commit": "high", "git.push": "critical"}
    assert all(t.capability_id for t in tools)


def test_15_la_whitelist_no_incluye_verbos_de_escritura():
    """La whitelist es de LECTURA. Que un verbo no esté es la garantía de `git.read`."""
    assert VERBOS_LECTURA.isdisjoint({
        "commit", "push", "add", "reset", "clean", "rm", "merge", "rebase", "fetch",
    })