"""R-03: el gate no puede anunciar una garantía de confianza que no aplica.

El defecto: `AutonomyGate` guardaba `confidence_threshold` y lo imprimía en el motivo de
la decisión con el texto `confianza=0.35 ≥ umbral=0.50`. Ese `≥` era LITERAL de una
cadena, no una comparación: en las 80+ construcciones del gate no existe ni una sola
expresión que compare `score` con `threshold`.

Traducido al funcionamiento real: nada filtraba por confianza. No era un control
débil, era un control inexistente con una etiqueta que decía lo contrario. Quien leyera
el log de una misión pensaría que ALEXIS decidió autonomously porque la confianza
superaba un umbral, y no había umbral.

Lo que se prueba es lo honesto, no lo que se querría: que el log ya no afirme una
comparación que no ocurre.
"""

from __future__ import annotations

from alexis.autonomy.gates import AutonomyGate
from alexis.contracts import (
    AutonomyLevel,
    Goal,
    Mission,
    MissionEnvelope,
    MissionState,
    PlanStep,
    RiskLevel,
)
from alexis.security.policy import PolicyEngine


def _mision(resultados=(), **envelope):
    return Mission(
        id="mision-r03",
        goal=Goal(objective="investigar el repositorio"),
        envelope=MissionEnvelope(
            objective="investigar el repositorio",
            autonomy=envelope.pop("autonomy", AutonomyLevel.AUTONOMOUS),
            allowed_actions=envelope.pop("allowed_actions", ["research", "execute"]),
            capabilities=envelope.pop("capabilities", ["fs.read"]),
            **envelope,
        ),
        state=MissionState.RUNNING,
        results=[{"success": True} for _ in resultados],
    )


def _paso(action="execute"):
    return PlanStep(
        id="investigar",
        description="investigar",
        action=action,
        risk=RiskLevel.LOW,
        capability="fs.read",
    )


def _decidir(mision, paso=None, gate=None):
    gate = gate or AutonomyGate()
    return gate.decide(mision, paso or _paso(), PolicyEngine())


# --------------------------------------------------------------------------- #
# 1 — el defecto congelado: el log anunciaba un filtro que no existía
# --------------------------------------------------------------------------- #

def test_01_el_motivo_ya_no_afirma_una_comparacion_inexistente():
    """La razón que sale del gate es lo que un humano lee para saber por qué pasó algo.

    Si dice `≥ umbral`, promete una comprobación. Ahora tiene que ser descriptiva.
    """
    decision = _decidir(_mision())

    assert "≥" not in decision.reason, (
        f"el motivo sigue anunciando una comparación que no ocurre: {decision.reason!r}"
    )
    assert "umbral=" not in decision.reason, (
        f"el motivo sigue pintando un umbral que no se compara: {decision.reason!r}"
    )
    assert "confianza" in decision.reason, (
        f"la confianza sigue siendo información útil y debe reportarse: {decision.reason!r}"
    )


# --------------------------------------------------------------------------- #
# 2 — el defecto era de fondo, no de redacción
# --------------------------------------------------------------------------- #

def test_02_no_existe_ninguna_comparacion_de_confianza_en_el_codigo():
    """La prueba que cierra el agujero de verdad, no el síntoma.

    Da igual qué diga el log: mientras no exista una comparación en el código, la
    garantía no existe. Esto falla si alguien reintrodujera la condición sin ajustar el
    mensaje, y al revés también.
    """
    import inspect

    fuente = inspect.getsource(AutonomyGate)
    lineas_comparacion = [
        linea for linea in fuente.splitlines()
        if "threshold" in linea
        and not linea.strip().startswith(("#", "\"", "'", "f\"", "f'"))
        and any(op in linea for op in ("<", ">", "==", "!="))
        and "confidence" not in linea
    ]
    assert not lineas_comparacion, (
        "ha aparecido una comparación con el umbral: si ahora el gate SÍ filtra por "
        f"confianza, hay que actualizar los mensajes y esta prueba: {lineas_comparacion}"
    )


def test_03_la_confianza_se_sigue_reportando_como_dato():
    """Quitar la mentira no puede ser quitar la información.

    La confianza es una medición útil: sirve para diagnosticar por qué una misión fue
    bloqueada. Lo que no puede hacer es fingir un filtro.
    """
    con_evidencia = _decidir(_mision(resultados=[1, 2, 3]))
    sin_evidencia = _decidir(_mision())

    assert "confianza" in con_evidencia.reason
    assert "confianza" in sin_evidencia.reason

    # Y el dato es real: con evidencia la confianza sube respecto a una misión recién empezada.
    def _score(texto):
        return float(texto.split("confianza actual=")[1].split(")")[0])

    assert _score(con_evidencia.reason) > _score(sin_evidencia.reason), (
        "la confianza reportada debe reflejar la evidencia: no es decorativa"
    )


def test_04_el_umbral_sobre_sigue_aceptandose_pero_no_promete_nada():
    """El parámetro se conserva —80+ llamadas lo constructores— pero documentado como no-control.

    Borrarlo sería más limpio, pero rompería a quien lo pase. Lo que no puede ser es que
    exista un parámetro llamado «umbral» que no hace nada sin que esté escrito.
    """
    gate = AutonomyGate(confidence_threshold=0.99)
    decision = _decidir(_mision(), gate=gate)

    assert gate.threshold == 0.99, "el valor se conserva"
    assert "umbral=" not in decision.reason, (
        "y el log no lo presenta como si se comparara"
    )


# --------------------------------------------------------------------------- #
# 3 — la decisión de diseño que queda pendiente
# --------------------------------------------------------------------------- #

def test_05_un_umbral_real_bloquearia_el_primer_paso_de_toda_mision():
    """POR QUÉ no se activa. No es descuido: es la consecuencia, medida.

    Con la fórmula de `MetaCognition`, una misión sin evidencia vale 0.35, y el umbral por
    defecto es 0.50. Aplicarlo de verdad significaría que el primer paso de CUALQUIER
    misión pide aprobación. Eso es un cambio de semántica de la autonomía, y por eso
    esta prueba existe: deja constancia de por qué la decisión está tomada y no tomada.

    Si algún día se decide activarlo, esta prueba falla y obliga a documentarlo.
    """
    from alexis.meta.cognition import MetaCognition

    score_mision_nueva = MetaCognition().assess(0, 0, 0).score
    assert score_mision_nueva < AutonomyGate().threshold, (
        "si una misión nueva ya alcanzara el umbral, activarlo sería inocuo y esta "
        "prueba debería replantearse: ya no valdría como justificación"
    )
    # Y se comprueba la consecuencia: con ese umbral, la decisión sería la que el gate
    # NO está tomando hoy.
    assert score_mision_nueva == 0.35, (
        f"la fórmula cambió (score inicial={score_mision_nueva}): recalcular antes de "
        f"activar el umbral, porque la justificación de esta decisión puede ya no valer"
    )