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


def test_04_el_parametro_muerto_se_elimino():
    """R-03 cerrado: ya no existe un knob que no hace nada.

    Antes este test afirmaba que el parámetro «se conserva pero no promete nada». La
    decisión de diseño que se dejó pendiente se ha resuelto por el otro lado: en vez de
    activar un umbral que bloquearía el primer paso de toda misión, se elimina el
    parámetro. Un control que no controla, documentado o no, sigue siendo una trampa para
    quien lo use creyendo que hace algo.

    Verificar que NO existe es parte del cierre: si alguien lo reintrodujera sin
    compararlo, el defecto volvería exactamente como estaba.
    """
    import inspect

    firma = inspect.signature(AutonomyGate.__init__)
    assert "confidence_threshold" not in firma.parameters, (
        "el parámetro muerto volvió: o se usa sin compararse —el defecto original— o se "
        "compara, que es una decisión de semántica que no se ha tomado"
    )

    gate = AutonomyGate()
    assert not hasattr(gate, "threshold"), (
        "y con él el atributo que se leía en el log como si fuera un filtro"
    )
    decision = _decidir(_mision(), gate=gate)
    assert "umbral" not in decision.reason, (
        f"el motivo sigue hablando de un umbral: {decision.reason!r}"
    )


def test_05_la_confianza_se_sigue_reportando_como_dato_real():
    """Quitar el umbral NO es quitar la medición.

    La confianza sigue calculándose y reportándose: es lo que permite diagnosticar por qué
    una acción se bloqueó. Lo que no se insinúa es que sea un control.
    """
    reason = _decidir(_mision(resultados=[1, 2, 3])).reason
    assert "confianza" in reason, (
        f"la confianza debe seguir en el motivo como dato: {reason!r}"
    )


# --------------------------------------------------------------------------- #
# 3 — la decisión de diseño que queda pendiente
# --------------------------------------------------------------------------- #
