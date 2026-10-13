"""R-08 — una aprobación pertenece a UN paso y no viaja a otro.

`approved_step_ids` ata el «sí» de una persona a un paso **por su identificador**, así que
el identificador *es* la frontera de autorización. Esto fija que:

1. dos pasos concretos distintos nunca compartan identidad;
2. aprobar uno no autorice otro;
3. un paso sin `step_id` siga siendo aprobable —que era imposible: se guardaba
   `"cognitive"` y se comprobaba `"cognitive-step"`.

Cada prueba tiene su contraprueba al final. Si alguien devuelve la identidad constante, o
vuelve a re-derivar la clave en el runtime, estas pruebas se rompen: eso es lo que las hace
pruebas y no descripciones.

Nada aquí doble la autoridad: `PolicyEngine` y `AutonomyGate` son los REALES, y lo que se
comprueba es que la identidad que llega a ellos es la que el operador aprobó.
"""

from __future__ import annotations

from alexis.autonomy.gates import AutonomyGate
from alexis.autonomy.mission import MissionEngine
from alexis.autonomy.step_identity import (
    IDENTIDAD_POR_DEFECTO,
    identidad_de_paso,
    nueva_identidad,
)
from alexis.cognition.loop import CognitiveRuntime
from alexis.cognition.state import Decision, NextAction
from alexis.contracts import AutonomyLevel, MissionEnvelope
from alexis.security.policy import PolicyEngine

ACCIONES = ["understand", "research", "execute", "verify", "respond"]


def _mision():
    return MissionEngine().create(
        "revisar el proyecto",
        MissionEnvelope(
            objective="revisar el proyecto",
            autonomy=AutonomyLevel.SUPERVISED,
            allowed_actions=list(ACCIONES),
        ),
    )


def _runtime():
    return CognitiveRuntime(policy=PolicyEngine(), gate=AutonomyGate(), executor=None,
                            verifier=None)


def _paso_que_pide_aprobacion():
    """Un paso que la autoridad REAL marca como `require_approval`.

    El disparador es `capability="git.push"`, no el riesgo: `_plan_step_for` fija
    `risk=MEDIUM` al construir el paso, así que un riesgo alto no se puede injectar por
    aquí. La regla que se quiere ejercitar es `git.push.requires_approval`
    (`policy_rules.py:44-50`), que es además la más restrictiva del catálogo.
    """
    return Decision(
        action=NextAction.EXECUTE_TOOL,
        rationale="publicar el cambio",
        capability="git.push",
        description="publicar",
    )


# --------------------------------------------------------------------------- #
# Aceptación 1-2: un paso sin step_id nace con identidad y se puede aprobar
# --------------------------------------------------------------------------- #

def test_01_un_paso_sin_step_id_nace_con_identidad_estable():
    rt = _runtime()
    mission = _mision()

    paso = rt._plan_step_for(_paso_que_pide_aprobacion(), [], mission=mission)

    assert paso.id == IDENTIDAD_POR_DEFECTO, (
        f"sin step_id la identidad debe ser la base estable, no un número de vuelta: "
        f"{paso.id!r}"
    )
    # Y es la MISMA si se reconstruye sin registrarla de nuevo: la identidad no depende de
    # cuántas veces se haya pedido.
    otra = rt._plan_step_for(_paso_que_pide_aprobacion(), [], mission=mission)
    assert otra.id.endswith("#2"), (
        f"reconstruir un paso nuevo debe desambiguar, no repetir: {otra.id!r}"
    )


def test_02_la_aprobacion_se_registra_y_el_paso_ya_aprobado_pasa():
    """El defecto A: un paso sin `step_id` era IMPOSIBLE de aprobar.

    Se guarda `pending_approval["step"]` y el gate busca esa misma clave. Antes eran dos
    constantes distintas y ninguna aprobación satisfacía a su propio paso.
    """
    rt = _runtime()
    mission = _mision()

    paso = rt._plan_step_for(_paso_que_pide_aprobacion(), [], mission=mission)

    # Lo que hace el runtime al pedir la aprobación: la clave es la del paso real.
    clave = paso.id
    mission.context["pending_approval"] = {"step": clave}
    # Lo que hace /approve: añade ESA clave.
    mission.context["approved_step_ids"] = [clave]

    decision = rt._authorize(mission, paso)

    assert decision.allowed, "la política real debe permitirlo dentro del envelope"
    assert decision.requires_approval, "sin la aprobación, el gate la exige"
    assert rt._aprobado_para_ejecutar(mission, paso), (
        "un paso ya aprobado no puede volver a pedir aprobación: es el defecto A, la "
        "aprobación guardada y la comprobada no eran la misma clave"
    )


# --------------------------------------------------------------------------- #
# Aceptación 3-7: replanificar no transfiere la aprobación
# --------------------------------------------------------------------------- #

def test_03_replanificar_no_transfiere_la_aprobacion_del_paso_anterior():
    """El recorrido entero, en el orden que fija el encargo.

    paso sin step_id → aprobar → replan → otro paso sin step_id → identidad distinta y
    vuelve a pedir permiso.
    """
    rt = _runtime()
    mission = _mision()

    # 1-2. Un paso sin step_id, aprobado explícitamente.
    primero = rt._plan_step_for(_paso_que_pide_aprobacion(), [], mission=mission)
    peticion = rt._authorize(mission, primero)
    assert peticion.requires_approval, "sin aprobar, este paso debe pedir permiso"

    mission.context["approved_step_ids"] = [primero.id]
    assert rt._aprobado_para_ejecutar(mission, primero), (
        "la aprobación del primer paso debe valer PARA ESE paso"
    )

    # 3-4. Replan: otro paso, también sin step_id.
    segundo = rt._plan_step_for(_paso_que_pide_aprobacion(), [], mission=mission)

    # 5. Identidad distinta.
    assert segundo.id != primero.id, (
        f"el replan creó un paso con la MISMA identidad ({segundo.id!r}): "
        f"la aprobación del anterior le viajaría"
    )

    # 6-7. Pide aprobación nueva y la del primero no lo autoriza.
    otra = rt._authorize(mission, segundo)
    assert otra.requires_approval, (
        "el segundo paso es una acción distinta y necesita su propia aprobación"
    )
    assert not rt._aprobado_para_ejecutar(mission, segundo), (
        "y la comprobación real de producción no lo da por aprobado"
    )
    assert primero.id in mission.context["approved_step_ids"]
    assert segundo.id not in mission.context["approved_step_ids"]


def test_04_la_identidad_sobrevive_a_un_reinicio():
    """El contador vive en la MISIÓN, no en la instancia del runtime.

    La aprobación se persiste en `mission.context`; si su frontera viviera en memoria, un
    reinicio devolvería el sistema al punto de partida y volvería a colisionar.
    """
    mission = _mision()
    primera = nueva_identidad(mission, None)

    # Simula el reinicio: mismo contexto serializado, runtime nuevo.
    import json

    mission.context = json.loads(json.dumps(mission.context))
    segunda = nueva_identidad(mission, None)

    assert segunda != primera, (
        f"tras recargar el contexto se repitió la identidad ({primera!r} == {segunda!r}): "
        f"el contador no es persistente y la aprobación volvería a colisionar"
    )


# --------------------------------------------------------------------------- #
# Aceptación 8: reutilización deliberada
# --------------------------------------------------------------------------- #

def test_05_reutilizar_un_id_ya_aprobado_produce_otra_identidad():
    """La contraprueba pedida: alguien reutiliza a propósito un id ya aprobado."""
    rt = _runtime()
    mission = _mision()

    # El modelo propone "leer-archivo" en su primer paso.
    primero = rt._plan_step_for(
        Decision(action=NextAction.EXECUTE_TOOL, rationale="a", step_id="leer-archivo"),
        [], mission=mission,
    )
    assert primero.id == "leer-archivo", "una id libre se respeta tal cual"
    mission.context["approved_step_ids"] = ["leer-archivo"]

    # Replan: el modelo reutiliza la MISMA id.
    segundo = rt._plan_step_for(
        Decision(action=NextAction.EXECUTE_TOOL, rationale="b", step_id="leer-archivo"),
        [], mission=mission,
    )

    assert segundo.id != "leer-archivo", (
        "la reutilización deliberada debe detectarse y desambiguarse: "
        f"quedó en {segundo.id!r} y heredaría la aprobación"
    )

    # Y lo que de verdad importa, comprobado con la lógica de PRODUCCIÓN: la clave aprobada
    # no ampara al paso reutilizado.
    assert not rt._aprobado_para_ejecutar(mission, segundo), (
        f"la aprobación de 'leer-archivo' ampara al paso nuevo ({segundo.id!r}): "
        f"el control no detectó la reutilización"
    )
    assert rt._aprobado_para_ejecutar(mission, primero), (
        "y la aprobación sigue amparando al paso ORIGINAL: corregir esto no puede "
        "romper las aprobaciones legítimas"
    )


def test_06_una_id_libre_se_respeta_tal_cual():
    """El otro lado: corregir esto NO puede renumerar ids que no chocan.

    Un plan que da ids propios los conserva, y con ellos su historial de aprobaciones.
    """
    assert identidad_de_paso("leer", {"otro"}) == "leer"
    assert identidad_de_paso("leer", set()) == "leer"


# --------------------------------------------------------------------------- #
# Contrapruebas sobre la PRODUCCIÓN
# --------------------------------------------------------------------------- #

def test_07_contraprueba_la_identidad_constante_reproduce_el_defecto():
    """Se ejecuta el comportamiento ANTERIOR a propósito y se comprueba que era malo.

    No se toca ningún fichero: la versión defectuosa se define aquí. Una contraprueba que
    pasara porque el sabotaje no se aplicó sería peor que no tenerla.
    """
    def _version_antigua(candidata, ya_usadas):
        return (candidata or "").strip() or IDENTIDAD_POR_DEFECTO

    ya = {"cognitive-step", "leer-archivo"}
    assert _version_antigua(None, ya) in ya, (
        "premisa del sabotaje: la identidad constante choca consigo misma"
    )
    assert _version_antigua("leer-archivo", ya) in ya, (
        "premisa del sabotaje: un id reutilizado también choca"
    )
    # Y con la corrección, ninguno de los dos choca.
    assert identidad_de_paso(None, ya) not in ya
    assert identidad_de_paso("leer-archivo", ya) not in ya


def test_08_contraprueba_la_red_derivacion_de_la_clave_producia_livelock():
    """La divergencia de `"cognitive"` / `"cognitive-step"`, reproducida.

    Con dos constantes distintas, lo que se guarda nunca satisface lo que se comprueba, y
    un paso cognitivo sin `step_id` queda pidiendo aprobación para siempre.
    """
    rt = _runtime()
    mission = _mision()
    paso = rt._plan_step_for(_paso_que_pide_aprobacion(), [], mission=mission)

    # Lo que el runtime ANTIGUO guardaba, re-derivado desde la decisión:
    decision = _paso_que_pide_aprobacion()
    clave_antigua = decision.step_id or "cognitive"
    mission.context["approved_step_ids"] = [clave_antigua]

    assert paso.id != clave_antigua, (
        "el sabotaje tiene que reproducir la divergencia y no lo hizo"
    )
    assert not rt._aprobado_para_ejecutar(mission, paso), (
        "con la clave re-derivada el paso sigue pidiendo permiso: eso es el livelock"
    )
    # Y con la clave correcta, no.
    mission.context["approved_step_ids"] = [paso.id]
    assert rt._aprobado_para_ejecutar(mission, paso), (
        "con la identidad real del paso, la aprobación sí satisface"
    )


# --------------------------------------------------------------------------- #
# 9 — el runtime REAL escribe la clave (nada de asignarla a mano en el test)
# --------------------------------------------------------------------------- #

def test_09_el_runtime_real_escribe_la_clave_del_paso_y_no_la_re_deriva():
    """Cierra el agujero que encontró la contraprueba.

    Las pruebas anteriores fijaban `pending_approval` a mano, así que no tocaban la línea
    del runtime que decide la clave: sabotearla no rompía NADA. Eso es cobertura falsa.

    Aquí se corre `_run_cognitive` DE VERDAD con un cognitive deaita whose only job is to
    return an approval outcome. Lo que se prueba no es cómo decide el cognitive —eso lo
    prueba `test_autonomy_core.py` con Policy y Gate reales— sino qué clave escribe el
    runtime al pedir la aprobación.
    """
    import asyncio

    from alexis.core.runtime import AlexisRuntime

    class _CognitiveDeBucle:
        """Produce UNA vez un outcome que exige aprobación, y sale del bucle después."""

        def __init__(self, paso):
            self.paso = paso
            self.knowledge = None
            self.salvado = None

        def knowledge_for(self, _mission):
            from alexis.cognition.state import KnowledgeState

            self.knowledge = KnowledgeState(objective="revisar el proyecto")
            return self.knowledge

        async def step(self, mission, knowledge, pending_steps=None, plan=None):
            from alexis.cognition.loop import StepOutcome
            from alexis.cognition.state import NextAction
            from alexis.contracts import MissionState

            self.salvado = StepOutcome(
                action=NextAction.EXECUTE_TOOL,
                knowledge=knowledge,
                decision=_paso_que_pide_aprobacion(),
                step_key=self.paso.id,
                done=True,
                mission_state=MissionState.WAITING_APPROVAL,
                requires_approval=True,
                error="policy exige aprobación",
            )
            return self.salvado

        def drain_rejections(self):
            return []

        def pending_steps(self, *_a, **_k):
            return [self.paso]

        def store_knowledge(self, *_a, **_k):
            return None

    async def _ciclo():
        rt = _runtime()
        mission = _mision()
        paso = rt._plan_step_for(_paso_que_pide_aprobacion(), [], mission=mission)

        cog = _CognitiveDeBucle(paso)
        clon = AlexisRuntime.__new__(AlexisRuntime)
        clon.cognitive = cog
        clon.events = _EventosFalsos()
        clon.mission_repo = None      # sin persistencia: la guarda de PAUSE no se opone
        clon.task_runner = None
        async def _commit(*_a, **_k):
            return None
        async def _persist_world(*_a, **_k):
            return None
        async def _persist_observations(*_a, **_k):
            return None
        clon._commit = _commit
        clon._persist_world = _persist_world
        clon._persist_observations = _persist_observations

        await clon._run_cognitive(mission, None, 0)
        return mission, paso, cog

    mission, paso, cog = asyncio.run(_ciclo())

    pendiente = mission.context.get("pending_approval")
    assert pendiente, "el runtime debió dejar la misión esperando aprobación"
    assert pendiente["step"] == paso.id, (
        "el runtime escribió una clave distinta de la del paso real: es exactamente el "
        f"defecto A. Escribió {pendiente['step']!r}, el paso es {paso.id!r}"
    )

    # Y el recorrido completo: el operador aprueba ESA clave (lo que hace `/approve`, que
    # añade `pending_approval["step"]` a `approved_step_ids`) y entonces la comprobación de
    # producción tiene que amparar al paso.
    rt = _runtime()
    assert not rt._aprobado_para_ejecutar(mission, paso), (
        "sin aprobar, el paso no puede estar aprobado"
    )
    mission.context["approved_step_ids"] = [pendiente["step"]]
    assert rt._aprobado_para_ejecutar(mission, paso), (
        "la clave que escribió el runtime no ampara al paso: la aprobación nunca surtiría "
        "efecto y el paso pediría permiso para siempre"
    )


class _EventosFalsos:
    """Bus mínimo: el bucle publica eventos y no hay almacenamiento en esta prueba."""

    async def publish(self, *_a, **_k):
        return None


# --------------------------------------------------------------------------- #
# 10 — la rama REAL de aprobación de step() propaga la identidad
# --------------------------------------------------------------------------- #

def test_10_step_real_devuelve_el_step_key_del_paso():
    """Cubre la línea que el test 9 no podía cubrir.

    El test 9 ejercita el runtime con un outcome construido a mano, así que la construcción
    del `StepOutcome` dentro de `CognitiveRuntime.step()` quedaba sin comprobar: quitarle el
    `step_key` no rompía NADA, y la contraprueba lo confirmó (0 tests rotos).

    Aquí se recorre la rama REAL: `step()` con Policy y Gate REALES y un plan que contiene
    un paso de aprobación. El executor NO debe llamarse nunca —la aprobación corta antes— y
    eso también se comprueba: un executor que corriera sería otro defecto.
    """
    import asyncio

    from alexis.cognition.state import KnowledgeState
    from alexis.contracts import MissionState, Plan, PlanStep, RiskLevel

    ejecutados: list[str] = []

    async def _execute(mission, step, decision=None, tool_name=None):
        ejecutados.append(step.id)
        raise AssertionError(
            f"el executor no debe ejecutarse sin aprobación (se llamó con {step.id!r})"
        )

    rt = CognitiveRuntime(
        policy=PolicyEngine(),
        gate=AutonomyGate(),
        executor=None,
        verifier=None,
        execute=_execute,
        model_router=None,
    )

    mission = _mision()
    mission.envelope.capabilities = ["git.push"]
    mission.envelope.allowed_actions = list(ACCIONES) + ["commit"]
    paso = PlanStep(
        id="publicar-codigo",
        description="publicar el cambio",
        action="commit",
        risk=RiskLevel.CRITICAL,
        agent="reasoner",
        capability="git.push",
        requires_approval=True,
    )
    plan = Plan(mission.id, [paso])

    outcome = asyncio.run(
        rt.step(mission, KnowledgeState(objective="publicar"),
                pending_steps=[paso], plan=plan)
    )

    assert outcome.mission_state is MissionState.WAITING_APPROVAL, (
        f"no se alcanzó la rama de aprobación: {outcome.mission_state!r}"
    )
    assert outcome.requires_approval is True
    assert not ejecutados, (
        f"el executor corrió sin aprobación: {ejecutados}"
    )
    assert outcome.step_key == "publicar-codigo", (
        f"el outcome no lleva la identidad del paso: {outcome.step_key!r}. Sin esto, "
        f"`pending_approval['step']` se escribiría con una constante y la aprobación "
        f"nunca satisfaría a su propio paso."
    )
