"""P0 §5.5 — la política del estado final: `COMPLETED` solo con objetivo verificado.

Este módulo es la respuesta a la pregunta "¿quién decide el estado final?". La respuesta es
una sola: la función `settle()` de aquí, y la invariante de `Mission.__setattr__` en
`alexis/contracts.py` es la que hace que ninguna otra respuesta sea posible.

La asimetría es deliberada:

- `NEEDS_VERIFICATION` significa "todavía no", no "falló". Un objetivo no demostrado deja la
  misión viva y explicable. `FAILED` queda para cuando hay un fallo de verdad.
- `BLOCKED` se reserva a la autoridad: policy, gate, aprobación o capability ausente.
- `COMPLETED` es el único estado que exige `GoalVerification.verified is True`.

`goal_is_confirmed()` revalida la verificación desde sus propios criterios antes de
autorizar nada. Así, un `GoalVerification(verified=True)` fabricado a mano, o uno que venga
serializado de una base de datos con `verified: true` pero sin evidencia, no puede abrir la
puerta: se vuelve a comprobar criterio por criterio. La autoridad es la **evidencia**, no
una bandera.
"""

from __future__ import annotations

from alexis.contracts import MissionState


#: Grados de evidencia que pueden sostener un criterio cumplido. `uncertainty`,
#: `assumption` e `inference` NO pueden: son exactamente lo contrario de una prueba.
#: Un `trusted=True` con uno de estos grados es una contradicción, y se trata como tal.
TRUSTWORTHY_GRADES = frozenset({"evidence", "fact"})

#: Una evidencia que autoriza un objetivo tiene que venir de una HERRAMienta que MIRA.
#: Sin el prefijo `tool:` no es una observación: es una declaración, y una declaración no
#: prueba nada por sí sola. Esta es la razón por la que la persistencia no puede
#: reautorizar por su cuenta: la fuente tiene que ser verificable por su forma, no por un
#: booleano que el almacenamiento pueda cambiar.
TOOL_SOURCE_PREFIX = "tool:"


def _evidence_provenance_is_sound(evaluation) -> bool:
    """¿La evidencia que sostiene este criterio es una observación de herramienta real?

    Exige DOS cosas, y las dos son estructurales:

    1. `source` empieza por `tool:` — lo que declara haber observado el mundo. Un claim
       del modelo, una nota del criterio o una etiqueta inventada no la tienen.
    2. Su grado es `evidence` o `fact` — una incertidumbre, una suposición o una
       inferencia no pueden sostener un `satisfied`, diga lo que diga su `trusted`.

    El `trusted` se sigue exigiendo (es el contrato de §5.3), pero ya no es lo ÚNICO que
    se mira: por sí solo era un booleano que cualquiera podía escribir.
    """
    for evidence in getattr(evaluation, "evidence", []) or []:
        if not getattr(evidence, "trusted", False):
            continue
        source = str(getattr(evidence, "source", "") or "")
        grade = str(getattr(evidence, "grade", "") or "")
        if source.startswith(TOOL_SOURCE_PREFIX) and grade in TRUSTWORTHY_GRADES:
            return True
    return False


def goal_is_confirmed(verification, mission=None) -> bool:
    """¿Esta verificación autoriza `COMPLETED`? Se revalida, no se trusts.

    Exige, en este orden:

    1. que sea una `GoalVerification` real y diga `verified=True`;
    2. que tenga al menos un criterio y **todos** estén `satisfied`;
    3. que cada criterio se sostenga con evidencia **fíable Y de procedencia sana**
       (`trusted` + `source` de herramienta + grado `evidence`/`fact`);
    4. si se le pasa la misión, que la verificación **sea de esa misión**: mismo
       `mission_id`, mismo objetivo y mismos criterios (§11). Sin este paso, la
       verificación de la misión A satisfied a la B.

    El punto 3 es el que cierra el ataque de persistencia: una fila manipulada puede
    escribir `trusted: true`, pero no puede hacer que un `source` inventado parezca una
    observación de herramienta ni que una `uncertainty` sea una prueba. Y el punto 4 es el
    que impide reutilizar una verificación entre misiones.

    `mission` es opcional para no romper los llamadores que sólo preguntan por la forma
    del objeto; donde importa —la invariante de `Mission`, `settle()` y la restauración
    desde la fila— se pasa siempre.
    """
    if verification is None:
        return False
    if type(verification).__name__ != "GoalVerification":
        return False
    if getattr(verification, "verified", None) is not True:
        return False
    evaluations = list(getattr(verification, "evaluations", []) or [])
    if not evaluations:
        return False
    for evaluation in evaluations:
        status = getattr(evaluation, "status", None)
        if getattr(status, "value", status) != "satisfied":
            return False
        if not _evidence_provenance_is_sound(evaluation):
            return False

    if mission is not None:
        from alexis.cognition.goal_verification import subject_matches

        if not subject_matches(verification, mission):
            return False
    return True


def settle(mission, verification) -> MissionState:
    """Única autoridad sobre el estado final. Devuelve el estado y lo aplica.

    `mission.state = COMPLETED` solo se ejecuta cuando `verification` realmente lo
    autoriza; en cualquier otro caso la misión queda en un estado honesto y el motivo
    queda escrito en el contexto para que la decisión sea auditable.
    """
    from alexis.cognition.goal_verification import GoalVerification

    if verification is not None and not isinstance(verification, GoalVerification):
        raise TypeError(
            "settle() solo acepta un GoalVerification del GoalVerifier; "
            f"recibió {type(verification).__name__}"
        )

    # La verificación se adjunta SIEMPRE, verificada o no: es el registro de por qué la
    # misión quedó como quedó, y es lo que la invariante de `Mission` consulta.
    if verification is not None:
        mission.goal_verification = verification

    if goal_is_confirmed(verification, mission):
        # También en el context: sin esto la fila persistida no lleva la verificación y
        # al recuperarla no se podría reconstruir un `completed` legítimo (§5.5).
        mission.context["goal_verification"] = verification.to_dict()
        mission.context["goal_verification_reason"] = verification.reason
        mission.state = MissionState.COMPLETED
        return mission.state

    mission.context["goal_verification"] = (verification.to_dict() if verification else None)
    mission.context["goal_verification_reason"] = _reason(verification)
    if verification is not None and getattr(verification, "unsatisfied", None) is not None:
        failing = verification.unsatisfied()
        mission.state = MissionState.BLOCKED if failing else MissionState.NEEDS_VERIFICATION
    else:
        mission.state = MissionState.NEEDS_VERIFICATION
    return mission.state


def _reason(verification) -> str:
    if verification is None:
        return (
            "no hay GoalVerifier en esta ejecución: sin verificación del objetivo no se "
            "puede declarar COMPLETED (P0 §5.5)"
        )
    reason = str(getattr(verification, "reason", "") or "")
    return reason or "el objetivo no está verificado"


__all__ = ["goal_is_confirmed", "settle"]
