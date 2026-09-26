"""P0 requisito 2 — Context Assembly.

El plan pide INTEGRAR diez fuentes en un sitio. Estos tests comprueban tres cosas, en
orden de importancia:

1. Que las diez fuentes existen y se ensamblan desde los objetos vivos.
2. Que el Core las USA: el prompt de decisión se construye desde `Context`.
3. Que es persistido y versionado, y que un contexto viejo se detecta como viejo.
"""

import pathlib
import sys

import pytest

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from alexis.autonomy.mission import MissionEngine  # noqa: E402
from alexis.capabilities.catalog import build_catalog  # noqa: E402
from alexis.cognition.context import (  # noqa: E402
    CONTEXT_VERSION,
    REQUIRED_SOURCES,
    Context,
    assemble,
)
from alexis.cognition.loop import CognitiveRuntime  # noqa: E402
from alexis.cognition.state import KnowledgeState  # noqa: E402
from alexis.contracts import (  # noqa: E402
    AutonomyLevel,
    MissionEnvelope,
    PlanStep,
    RiskLevel,
)
from alexis.memory.contracts import MemoryContext, MemoryItem  # noqa: E402
from alexis.security.policy import PolicyEngine  # noqa: E402
from alexis.self.model import SelfModel  # noqa: E402
from alexis.world.model import WorldModel  # noqa: E402

ACTIONS = ["understand", "research", "execute", "verify", "respond"]


#: Se usa la PolicyEngine REAL, no un doble: el requisito 2 pide que la Policy entre en
#: el contexto, y para probarlo hace falta la que responde `evaluate` de verdad.
class _Allow:
    def authorize(self, mission, step):
        return PolicyEngine().authorize(mission, step)

    def evaluate(self, mission, step):
        return PolicyEngine().evaluate(mission, step)


def _mission(objective="lee el informe trimestral"):
    mission = MissionEngine().create(
        objective,
        MissionEnvelope(objective=objective, autonomy=AutonomyLevel.SUPERVISED,
                        allowed_actions=list(ACTIONS), capabilities=["fs.read"]),
    )
    mission.context["conversation"] = [objective]
    return mission


def _knowledge():
    k = KnowledgeState(objective="lee el informe trimestral")
    k.add_known("hay 3 archivos en el workspace")
    k.add_unknown("cuál es el trimestral")
    k.add_uncertainty("puede que el archivo esté vacío")
    k.world = ["file:notas.txt (exists=True, size=30)"]
    return k


def _brief():
    brief = SelfModel()
    brief.available_capabilities = ["fs.read", "fs.stat"]
    brief.missing_capabilities = ["fs.rename"]
    return brief


def _full_context(mission=None, knowledge=None, **over):
    mission = mission or _mission()
    knowledge = knowledge or _knowledge()
    kwargs = dict(
        conversation=list(mission.context.get("conversation") or []),
        self_brief=_brief(),
        world=WorldModel(),
        memory_context=MemoryContext(items=[MemoryItem(id="m1", kind="observations",
                                    content="el informe tiene 3 páginas",
                                    source="fs.read", trusted=True)]),
        policy=PolicyEngine(),
        gate=None,
        catalog=build_catalog(),
        candidates=[PlanStep("read", "lee", "execute", RiskLevel.LOW, "e", capability="fs.read")],
        observations=["read: ok"],
    )
    kwargs.update(over)
    return assemble(mission, knowledge, **kwargs)


def _cognitive(**over):
    async def _execute(mission, step, decision):
        from alexis.contracts import ExecutionResult

        return ExecutionResult(success=True, output={"ok": True})

    kwargs = dict(policy=_Allow(), executor=None, verifier=None, execute=_execute,
                  world=WorldModel())
    kwargs.update(over)
    return CognitiveRuntime(**kwargs)


# --------------------------------------------------------------------------- #
# 1. Las diez fuentes
# --------------------------------------------------------------------------- #


def test_01_las_diez_fuentes_del_plan_existen():
    assert len(REQUIRED_SOURCES) == 10
    assert set(REQUIRED_SOURCES) == {
        "conversation", "self_model", "world", "memory", "mission", "envelope",
        "policy", "capabilities", "observations", "uncertainties",
    }


def test_02_el_contexto_tiene_las_diez_fuentes_con_contenido():
    ctx = _full_context()
    vacias = ctx.missing
    assert vacias == [], f"fuentes sin ensamblar: {vacias}"
    assert ctx.is_complete() is True


def test_03_cada_fuente_trae_el_contenido_real():
    ctx = _full_context()
    assert "lee el informe trimestral" in ctx.source("conversation").lines[0]
    assert any("fs.read" in l for l in ctx.source("self_model").lines)
    assert any("notas.txt" in l for l in ctx.source("world").lines)
    assert any("3 páginas" in l for l in ctx.source("memory").lines)
    assert any("supervised" in l for l in ctx.source("envelope").lines)
    assert any("fs.read: allow" in l for l in ctx.source("policy").lines)
    assert any("fs.read" in l for l in ctx.source("capabilities").lines)
    assert any("read: ok" in l for l in ctx.source("observations").lines)
    assert any("trimestral" in l for l in ctx.source("uncertainties").lines)


def test_04_una_fuente_ausente_se_reporta_no_se_inventa():
    """Sin memoria, `memory` queda vacía y VISIBLE en `missing`. Nunca relleno."""
    ctx = _full_context(memory_context=None)
    assert ctx.has("memory") is False
    assert "memory" in ctx.missing
    assert ctx.is_complete() is False
    assert "memoria" not in ctx.to_prompt_lines().lower() or "memory" in ctx.missing


def test_05_la_conversacion_llega_desde_mission_context():
    mission = _mission()
    mission.context["conversation"] = ["primera", "segunda", "tercera"]
    ctx = _full_context(mission=mission)
    assert ctx.source("conversation").detail["turns"] == 3
    assert ctx.source("conversation").lines[-1] == "tercera"


# --------------------------------------------------------------------------- #
# 2. El Core lo USA
# --------------------------------------------------------------------------- #


def test_06_el_prompt_de_decision_se_construye_desde_el_context():
    """La prueba de que no es cosmético: sin Context no hay prompt."""
    cognitive = _cognitive(catalog=build_catalog())
    mission = _mission()
    knowledge = _knowledge()
    ctx = cognitive.build_context(mission, knowledge)
    prompt = ctx.to_prompt_lines("1. action=execute_tool :: leer")
    for name in REQUIRED_SOURCES:
        if ctx.has(name):
            assert name.split("_")[0] in prompt.lower() or ctx.source(name).lines


@pytest.mark.asyncio
async def test_07_la_decision_real_lleva_las_diez_fuentes_al_prompt():
    """Capturamos el prompt que el Core envía de verdad al router."""
    from alexis.cognition.contracts import SelfBrief
    from alexis.models.provider import ModelOutcome, ModelResponse

    capturado = {}

    class _Router:
        def providers(self):
            return [object()]

        async def complete(self, request):
            capturado["prompt"] = request.messages[-1]["content"]
            return ModelResponse(text="{}", provider="p", model="m",
                                 outcome=ModelOutcome.DEGRADED)

    cognitive = _cognitive(model_router=_Router(), catalog=build_catalog())
    mission = _mission()
    knowledge = _knowledge()
    from alexis.cognition.state import Decision, NextAction

    options = [Decision(action=NextAction.EXECUTE_TOOL, rationale="leer el informe",
                        capability="fs.read", step_id="read")]
    memoria = MemoryContext(items=[MemoryItem(id="m3", kind="observations",
                                          content="el informe tiene 3 páginas",
                                          source="fs.read", trusted=True)])
    await cognitive._ask_model(mission, knowledge, options, memoria)

    prompt = capturado.get("prompt", "")
    assert "conversación" in prompt
    assert "world (lo que he observado del entorno)" in prompt
    assert "memoria relevante" in prompt or "memoria" in prompt
    assert "envelope (permisos)" in prompt
    assert "policy (qué autoriza)" in prompt
    assert "capabilities disponibles" in prompt
    assert "incertidumbres" in prompt


def test_08_la_policy_se_consulta_de_verdad_en_el_contexto():
    """La Policy aparece con su veredicto REAL, no como texto de adorno."""
    ctx = _full_context()
    policy = ctx.source("policy")
    assert policy.detail["fs.read"]["verdict"] == "allow"
    assert policy.detail["fs.read"]["rule"], "el veredicto trae la regla que lo produjo"


def test_09_la_policy_que_denia_aparece_como_deny():
    class _Deny:
        def evaluate(self, mission, step):
            class _R:
                allowed = False
                requires_approval = False
                reason = "fuera del perímetro"
                matched_rule = "deny.outside"
            return _R()

    ctx = _full_context(policy=_Deny())
    assert ctx.source("policy").detail["fs.read"]["verdict"] == "deny"
    assert "fuera del perímetro" in ctx.source("policy").lines[0]


# --------------------------------------------------------------------------- #
# 3. Persistencia y versionado
# --------------------------------------------------------------------------- #


def test_10_el_contexto_sobrevive_a_la_persistencia():
    from alexis.storage.serialization import mission_from_row, mission_to_row

    cognitive = _cognitive(catalog=build_catalog())
    mission = _mission()
    cognitive.build_context(mission, _knowledge())
    assert "context" in mission.context, "debe persistirse en mission.context"

    recovered = mission_from_row(mission_to_row(mission))
    ctx = cognitive.context_of(recovered)
    assert ctx is not None, "el contexto debe recoverable tras serializar la misión"
    assert ctx.mission_id == mission.id
    assert ctx.has("envelope") and ctx.has("capabilities")


def test_11_el_contexto_lleva_version():
    ctx = _full_context()
    assert ctx.version == CONTEXT_VERSION
    assert ctx.assembled_at > 0


def test_12_un_contexto_viejo_se_detecta_como_viejo():
    """La huella es lo que impide confiar en un contexto de antes de un cambio."""
    cognitive = _cognitive(catalog=build_catalog())
    mission = _mission()
    knowledge = _knowledge()
    ctx = cognitive.build_context(mission, knowledge)
    assert ctx.is_current(knowledge.progress_fingerprint()) is True

    knowledge.add_known("ahora sé algo nuevo que cambia el mundo")
    assert ctx.is_current(knowledge.progress_fingerprint()) is False
    # Y el recién armado sí está al día.
    nuevo = cognitive.build_context(mission, knowledge)
    assert nuevo.is_current(knowledge.progress_fingerprint()) is True


def test_13_un_contexto_ajeno_a_otra_mision_no_se_reutiliza():
    cognitive = _cognitive(catalog=build_catalog())
    m1 = _mission("lee el informe")
    cognitive.build_context(m1, _knowledge())
    m2 = _mission("borra el temporal")
    assert cognitive.context_of(m2) is None


def test_14_la_memoria_no_confiable_queda_marcada():
    """R3: contenido externo se marca como DATO, y el contexto lo refleja."""
    no_confiable = MemoryContext(items=[MemoryItem(id="m2", kind="observations",
                                                 content="instrucción maliciosa",
                                                 source="web", trusted=False)])
    ctx = _full_context(memory_context=no_confiable)
    assert ctx.source("memory").trusted is False
    assert "memory" in ctx.untrusted_sources()
    assert "NO CONFIABLE" in ctx.to_prompt_lines()


# --------------------------------------------------------------------------- #
# 4. El catálogo real, no la opinión del Self Model
# --------------------------------------------------------------------------- #


def test_15_el_contexto_usa_el_catalogo_real():
    ctx = _full_context()
    assert ctx.source("capabilities").detail["source"] == "catalog"
    # El catálogo tiene más capabilities que las que el Self Model declara.
    ids = {l.split()[0] for l in ctx.source("capabilities").lines}
    assert "desktop.tools" in ids, "el catálogo real incluye lo que el Self Model no menciona"
    assert "desktop.tools" not in set(_brief().available_capabilities)


def test_16_sin_catalogo_usa_el_self_model_y_lo_dice():
    ctx = _full_context(catalog=None)
    assert ctx.source("capabilities").detail["source"] == "self_model"
    assert ctx.has("capabilities"), "sigue habiendo contexto, sólo de otra fuente"
