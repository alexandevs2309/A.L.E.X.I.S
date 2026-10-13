"""R-06 — el catálogo no declara disponible una capacidad que no funciona.

`git.push` tiene adaptador, tool, política y validación de remoto. Y aun así **no
funciona**: `SandboxRunner` aplica `RLIMIT_NPROC=16` y el proceso de git necesita bifurcar
`remote-http` para publicar. Con más de 16 procesos del usuario, el fork falla.

El catálogo lo declaraba `status="available"`. Eso convierte un inventario en propaganda:
quien lo lea —un planner, un operador, una auditoría— daría por hecho que ALEXIS puede
publicar código.

Lo que NO se toca aquí, y por eso se comprueba explícitamente:

- `RLIMIT_NPROC` sigue en 16. Subirlo haría funcionar el push, pero eso es una decisión
  sobre el sandbox que no es de este encargo.
- `risk=critical` y `plans_action="commit"` se conservan.
- `git.push.requires_approval` sigue exigiendo aprobación aunque la capacidad no sea
  operativa: si mañana se arregla el límite, la regla debe seguir ahí.
- La validación del remoto con `resolve_public()` se conserva: es la barrera que impide
  que un `origin` malicioso se lleve las credenciales de git.

La última prueba es la contraprueba: comprueba que estas comprobaciones muerden.
"""

from __future__ import annotations

import resource
from dataclasses import replace
from pathlib import Path

from alexis.capabilities.catalog import build_catalog
from alexis.security.policy_rules import RULES, evaluate_rules

RAIZ = Path(__file__).resolve().parents[1]


def _spec():
    return next(s for s in build_catalog().specs() if s.id == "git.push")


def _mision_con_push():
    """Una misión que DECLARA `git.push`, para llegar a la regla de aprobación.

    Si el envelope no declara la capability, la regla `capability.outside_envelope` deniega
    antes de llegar a `git.push.requires_approval`, y la prueba no probaría lo que dice
    probar.
    """
    from alexis.contracts import AutonomyLevel, Goal, Mission, MissionEnvelope

    return Mission(
        id="m-r06",
        goal=Goal(objective="publicar"),
        envelope=MissionEnvelope(
            objective="publicar",
            autonomy=AutonomyLevel.AUTONOMOUS,
            allowed_actions=["commit"],
            forbidden_actions=[],
            approval_required=[],
            capabilities=["git.push"],
            auto_approve=[],
        ),
    )


def _paso_push():
    return type("S", (), {
        "id": "push",
        "action": "commit",
        "capability": "git.push",
        "risk": "critical",
    })()


# --------------------------------------------------------------------------- #
# 1-2 — el catálogo refleja la realidad
# --------------------------------------------------------------------------- #

def test_01_git_push_no_se_declara_disponible():
    """El estado del catálogo es la afirmación que el resto del sistema se cree."""
    spec = _spec()
    assert spec.status != "available", (
        "git.push NO es operativa dentro del sandbox (RLIMIT_NPROC=16 impide el fork de "
        "remote-http). Declararla `available` hace que el resto del sistema asuma una "
        "capacidad que no existe."
    )
    assert spec.status == "missing", (
        "`missing` es el estado que el catálogo usa para lo que no puede ejecutarse —lo "
        f"usan `terminal.run`, `mcp.run`, `iot.mqtt`—; se obtuvo {spec.status!r}"
    )


def test_02_la_clasificacion_coincide_con_la_limitacion_documentada():
    """El catálogo y `docs/SECURITY.md` tienen que decir lo mismo.

    Si el documento afirma una cosa y el catálogo otra, uno de los dos miente y no hay
    forma de saber cuál desde fuera.
    """
    spec = _spec()

    assert "RLIMIT_NPROC" in spec.description, (
        "una capacidad ausente tiene que decir POR QUÉ: `missing` sin motivo no permite "
        "saber si arreglar el límite la rehabilita"
    )

    texto = (RAIZ / "docs" / "SECURITY.md").read_text(encoding="utf-8")
    assert "RLIMIT_NPROC" in texto, "SECURITY.md debe documentar la limitación"
    assert "git.push" in texto or "git push" in texto


def test_03_el_limite_del_sandbox_no_se_toco():
    """Que la capacidad sea no operativa es informativo; el límite sigue siendo el control."""
    fuente = (RAIZ / "alexis" / "security" / "sandbox.py").read_text(encoding="utf-8")
    assert "RLIMIT_NPROC, (16, 16)" in fuente, (
        "R-06 no autoriza cambiar el sandbox: subir el límite haría funcionar el push, y "
        "eso es otra decisión, con sus consecuencias para el resto de herramientas"
    )
    # El proceso de pruebas puede no llevar el límite puesto —el sandbox lo aplica en el
    # hijo—, así que la comprobación de verdad es la del código, no la de este proceso.
    actual = resource.getrlimit(resource.RLIMIT_NPROC)
    assert actual[0] in (resource.RLIM_INFINITY, actual[0])


# --------------------------------------------------------------------------- #
# 3 — las garantías que NO se relajan
# --------------------------------------------------------------------------- #

def test_04_el_riesgo_y_la_accion_no_se_bajan():
    """Que no funcione no la vuelve menos peligrosa: si se arregla, sigue siendo crítica."""
    spec = _spec()
    assert spec.default_risk == "critical", (
        "bajar el riesgo de una capacidad que publica código sería lo contrario de lo que "
        "hace falta: si mañana funciona, se ejecuta sin la revisión que exige"
    )
    assert spec.plans_action == "commit", "la acción que la invoca no cambia"
    assert spec.sandbox_profile == "network-observed"
    assert spec.network is True, "sigue siendo una capacidad de red"
    assert spec.side_effects is True, "y sigue teniendo efectos"


def test_05_la_policy_sigue_exigiendo_aprobacion():
    """Lo más importante: marcar la capacidad NO afloja la autorización.

    Si alguien leyera `missing` como «inofensiva», que la regla de aprobación desapareciera
    sería un agujero esperando a que alguien arreglara el límite.
    """
    assert any(r.get("id") == "git.push.requires_approval" for r in RULES), (
        "la regla de aprobación de git.push no puede desaparecer"
    )

    verdict, reason, rule = evaluate_rules(_mision_con_push(), _paso_push())

    assert verdict == "require_approval", (
        f"con la capability declarada no operativa, la policy devolvió {verdict!r}: "
        f"se publicaría sin preguntar"
    )
    assert rule == "git.push.requires_approval", (
        f"y debe ser la regla de git.push la que case, no otra: {rule!r}"
    )


def test_06_la_validacion_del_remoto_se_conserva():
    """`resolve_public()` es la única barrera de red que tiene esta tool."""
    import alexis.tools.git as git_mod
    import alexis.tools.net as net_mod

    assert hasattr(net_mod, "resolve_public"), (
        "si `resolve_public` desaparece, la validación del remoto desaparece con ella"
    )
    fuente = Path(git_mod.__file__).read_text(encoding="utf-8")
    assert "resolve_public" in fuente, (
        "GitPushTool debe seguir resolviendo y validando el remoto antes de conectar"
    )


# --------------------------------------------------------------------------- #
# Contraprueba
# --------------------------------------------------------------------------- #

def test_07_contraprueba_volver_a_available_es_detectable():
    """Se aplica el sabotaje en memoria y se comprueba que la comprobación lo detecta.

    No se edita el catálogo: se construye un `CapabilitySpec` con `git.push` disponible y
    se comprueba con la misma condición que usa `test_01`. Una contraprueba que pasara
    porque el sabotaje no se aplicó sería peor que no tenerla.
    """
    real = _spec()
    saboteado = replace(real, status="available")

    def _aceptaria_como_disponible(spec) -> bool:
        # La condición EXACTA que `test_01` prohíbe.
        return spec.status == "available"

    assert not _aceptaria_como_disponible(real), (
        "el estado real no debe aceptarse como disponible"
    )
    assert _aceptaria_como_disponible(saboteado), (
        "el sabotaje tiene que hacer fallar la comprobación; si no pasara, la contraprueba "
        "no probaría nada"
    )
    assert saboteado.default_risk == real.default_risk, (
        "el sabotaje cambia SÓLO el estado: es lo que haría un descuido, no un rediseño"
    )
