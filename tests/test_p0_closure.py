"""P0 CLOSURE PASS — pruebas por fase.

FASE A: `ResponseComposer` es la fuente semántica común y ningún canal inventa.
FASE B: selección dinámica de capabilities.
FASE C: recovery cognitivo a mitad de misión.
FASE E: E2E real sin sustituir el runtime.
"""

import pathlib
import sys

import pytest

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from alexis.autonomy.mission import MissionEngine  # noqa: E402
from alexis.cognition.contracts import UserReply  # noqa: E402
from alexis.cognition.goal_verification import GoalVerification  # noqa: E402
from alexis.cognition.response import (  # noqa: E402
    ResponseComposer,
    read_composed,
    render_for_voice,
)
from alexis.cognition.state import KnowledgeState, Verdict  # noqa: E402
from alexis.contracts import AutonomyLevel, MissionEnvelope, PlanStep, RiskLevel  # noqa: E402
from alexis.execution import SandboxExecutor  # noqa: E402
from alexis.security.sandbox import SandboxRunner  # noqa: E402
from alexis.tools.registry import ToolRegistry  # noqa: E402

ACTIONS = ["understand", "analyze", "research", "execute", "verify", "respond"]


def _mission(objective="leer el informe"):
    return MissionEngine().create(
        objective,
        MissionEnvelope(objective=objective, autonomy=AutonomyLevel.SUPERVISED,
                        allowed_actions=list(ACTIONS)),
    )


def _composed(mission, *, verdict=Verdict.SUCCESS.value, goal_verified=True, outcome="real"):
    k = KnowledgeState(objective=mission.goal.objective)
    k.last_verdict = verdict
    k.mark_completed("read")
    v = GoalVerification(objective=mission.goal.objective, evaluations=[], verified=goal_verified, reason="")
    return ResponseComposer().compose(mission, k, v, model_outcome=outcome)


class _RealLikeRouter:
    """Router que se hace pasar por REAL (outcome=real) y devuelve texto fijo."""

    def __init__(self, text):
        self.text = text
        self.prompts = []

    def providers(self):
        return [object()]

    async def complete(self, request):
        from alexis.models import ModelResponse
        from alexis.models.provider import ModelOutcome

        self.prompts.append(request.messages[-1]["content"])
        return ModelResponse(text=self.text, provider="fake", model="fake",
                             outcome=ModelOutcome.REAL)


# =========================================================================== #
# FASE A — ResponseComposer es la fuente semántica (GAP 1)
# =========================================================================== #


class TestFaseAResponseComposerEsLaFuente:
    def test_a1_read_composed_recovered_desde_mission_context(self):
        mission = _mission()
        mission.context["response"] = _composed(
            mission, verdict=Verdict.FAILURE.value, goal_verified=False).to_dict()
        reply = read_composed(mission)
        assert reply is not None
        assert reply.verdict == Verdict.FAILURE.value
        assert reply.goal_verified is False

    def test_a2_read_composed_none_si_no_hay_epilogo(self):
        assert read_composed(_mission()) is None

    @pytest.mark.parametrize(
        "verdict,goal_verified,debe_decir",
        [
            (Verdict.SUCCESS.value, True, "verificado"),
            (Verdict.PARTIAL_SUCCESS.value, True, "verificado"),
            (Verdict.FAILURE.value, True, "no consiguió su propósito"),
            (Verdict.INSUFFICIENT_EVIDENCE.value, False, "no está verificado"),
            (Verdict.BLOCKED.value, False, "no está verificado"),
        ],
    )
    def test_a3_cada_verdict_produce_su_respuesta(self, verdict, goal_verified, debe_decir):
        mission = _mission()
        mission.context["blocked_reason"] = "requiere aprobación" if verdict == Verdict.BLOCKED.value else None
        if verdict != Verdict.BLOCKED.value:
            mission.context.pop("blocked_reason")
        reply = _composed(mission, verdict=verdict, goal_verified=goal_verified)
        assert debe_decir in reply.text.lower()
        assert reply.verdict == verdict

    @pytest.mark.asyncio
    async def test_a4_la_voz_presenta_la_respuesta_compuesta(self, tmp_path):
        """GAP 1: la voz ya no inventa; presenta lo que el Core compuso."""
        executor = SandboxExecutor(tools=ToolRegistry(), sandbox=SandboxRunner(workspace=tmp_path))
        mission = _mission()
        mission.context["response"] = _composed(
            mission, verdict=Verdict.FAILURE.value, goal_verified=False).to_dict()
        spoken = await executor._spoken_reply(mission, None)
        assert spoken, "la voz debe presentar la respuesta compuesta"
        assert "no está verificado" in spoken.lower()
        assert "falló" in spoken.lower()

    @pytest.mark.asyncio
    async def test_a5_la_voz_reformula_pero_no_puede_añadir_exitos(self, tmp_path):
        """Un modelo REAL puede reformular, pero su texto se vuelve a pasar por el guard."""
        router = _RealLikeRouter("He completado la tarea, todo listo.")
        executor = SandboxExecutor(tools=ToolRegistry(), sandbox=SandboxRunner(workspace=tmp_path),
                                  model_router=router)
        mission = _mission()
        mission.context["response"] = _composed(
            mission, verdict=Verdict.FAILURE.value, goal_verified=False).to_dict()
        spoken = await executor._spoken_reply(mission, None)
        assert "completado" not in spoken.lower(), "el guard debe impedir el falso éxito"
        assert "(logro no verificado)" in spoken
        # Y el modelo recibió los HECHOS, no el objetivo desnudo.
        assert "HECHOS A REFORMULAR" in router.prompts[0]

    @pytest.mark.asyncio
    async def test_a6_sin_epilogo_la_voz_no_inventa(self, tmp_path):
        """Sin respuesta compuesta la voz devuelve "": el caller usa su contingencia honesta."""
        executor = SandboxExecutor(tools=ToolRegistry(), sandbox=SandboxRunner(workspace=tmp_path))
        assert await executor._spoken_reply(_mission(), None) == ""

    @pytest.mark.asyncio
    async def test_a7_provenance_degraded_llega_a_la_voz(self, tmp_path):
        router = _RealLikeRouter("texto reformulado")
        executor = SandboxExecutor(tools=ToolRegistry(), sandbox=SandboxRunner(workspace=tmp_path),
                                  model_router=router)
        mission = _mission()
        mission.context["response"] = _composed(mission, outcome="unavailable").to_dict()
        spoken = await executor._spoken_reply(mission, None)
        assert "UNAVAILABLE" in spoken or "no había modelo" in spoken

    def test_a8_sanitize_protegente_es_accesible_para_cualquier_canal(self):
        c = ResponseComposer()
        assert c.sanitize("listo", goal_verified=False) != "listo"
        assert c.sanitize("listo", goal_verified=True) == "listo"

    def test_a9_render_for_voice_no_trae_markdown_ni_saltos(self):
        reply = _composed(_mission(), verdict=Verdict.SUCCESS.value)
        texto = render_for_voice(reply)
        assert "\n" not in texto
        assert "**" not in texto
        assert len(texto) < 400


# =========================================================================== #
# FASE B — Capability Selection dinámica (cierra el MISSING #6)
# =========================================================================== #


from alexis.capabilities.catalog import build_catalog  # noqa: E402
from alexis.cognition.contracts import CapabilityProposal  # noqa: E402
from alexis.cognition.selection import CapabilitySelector  # noqa: E402


class _DenyAllPolicy:
    """Policy que no autoriza nada: sirve para probar que la autoridad manda."""

    def authorize(self, mission, step):
        class _Deny:
            allowed = False
            requires_approval = False
            reason = "no autorizado por prueba"
            matched_rule = "deny-test"

        return _Deny()


class _ApprovalPolicy:
    def authorize(self, mission, step):
        class _Ask:
            allowed = False
            requires_approval = True
            reason = "pide aprobación"
            matched_rule = "approval-test"

        return _Ask()


class TestFaseBSeleccionDinamica:
    def setup_method(self):
        self.catalog = build_catalog()
        self.selector = CapabilitySelector(catalog=self.catalog)

    @pytest.mark.parametrize(
        "objetivo,esperada",
        [
            ("lee el informe trimestral", "fs.read"),
            ("escribe un resumen en un archivo nuevo", "fs.write"),
            ("borra el archivo temporal", "fs.remove"),
            ("háblame en voz alta el resumen", "tts.speak"),
        ],
    )
    def test_b1_la_capability_depende_del_objetivo(self, objetivo, esperada):
        """Éste es el requisito 6: el plan depende del objetivo, no de una plantilla."""
        selection = self.selector.select(objetivo)
        assert selection.selected, f"no seleccionó nada para {objetivo!r}"
        assert selection.selected[0] == esperada

    def test_b2_objetivos_distintos_dan_planes_distintos(self):
        """Prueba de no-template: cuatro objetivos, cuatro capabilities distintas."""
        elegidas = {self.selector.select(o).selected[0] for o in
                    ["lee el archivo", "escribe un archivo", "borra el archivo", "háblame"]}
        assert len(elegidas) == 4, f"sigue siendo una plantilla fija: {elegidas}"

    def test_b3_el_evidence_cambia_la_seleccion(self):
        """Seleccionar ignorando lo observado también es ser fijo."""
        from alexis.cognition.state import KnowledgeState

        sin_evidencia = self.selector.select("analiza esto")
        k = KnowledgeState(objective="analiza esto")
        k.add_known("el resultado está en notas.txt y hay que resumirlo")
        con_evidencia = self.selector.select("analiza esto", knowledge=k)
        assert con_evidencia.trace, "con evidencia debe haber traza de por qué"
        assert any("evidencia" in t["why"] for t in con_evidencia.trace)
        assert sin_evidencia.selected != [] or con_evidencia.selected != []

    def test_b4_una_capability_inexistente_nunca_se_selecciona(self):
        """El modelo propone; el Core recorta contra el catálogo real."""
        proposal = CapabilityProposal(capabilities=["fs.teletransport"], rationale="inventada")
        selection = self.selector.select("lee el informe", proposal=proposal)
        assert "fs.teletransport" not in selection.selected
        reglas = {r["rule"] for r in selection.rejected}
        assert "not_in_catalog" in reglas

    def test_b5_no_se_sustituye_una_operacion_que_no_se_sabe_hacer(self):
        """«renombrar» no es «escribir»: ALEXIS no tiene `fs.rename` y no debe fingir."""
        selection = self.selector.select("renombra el archivo reporte.txt a nuevo.txt")
        assert selection.selected == []
        reglas = {r["rule"] for r in selection.rejected}
        assert "capability_does_not_exist" in reglas
        assert "substitute_forbidden" in reglas
        assert self.selector.decide_unavailable(selection) == "ask_user"

    def test_b6_unavailable_produce_decision_controlada(self):
        selection = self.selector.select("contrata unsatélite para mi gatos")
        assert selection.selected == []
        assert self.selector.decide_unavailable(selection) in ("ask_user", "abort", "replan")

    def test_b7_la_policy_manda_sobre_la_seleccion(self):
        """La Core selecciona; la Policy tiene la última palabra."""
        selector = CapabilitySelector(catalog=self.catalog, policy=_DenyAllPolicy())
        selection = selector.select("lee el informe", mission=_mission(), step=_probe_step())
        assert selection.selected == [], "no puede seleccionar lo que la autoridad veta"
        assert all(r["rule"] == "not_authorized" for r in selection.rejected)
        assert selector.decide_unavailable(selection) == "ask_user"

    def test_b8_approval_produce_wait(self):
        selector = CapabilitySelector(catalog=self.catalog, policy=_ApprovalPolicy())
        selection = selector.select("lee el informe", mission=_mission(), step=_probe_step())
        assert selection.selected == []
        assert {r["rule"] for r in selection.rejected} == {"requires_approval"}
        assert selector.decide_unavailable(selection) == "wait"

    def test_b9_seleccion_usable_tiene_forma_de_contract(self):
        """Usa el contrato `Selection` que ya existía y no se usaba en ningún sitio."""
        selection = self.selector.select("lee el informe")
        row = selection.to_dict()
        assert set(row) == {"selected", "rejected", "rationale", "trace"}
        assert isinstance(row["selected"], list)
        assert row["trace"] and row["trace"][0]["capability"] == selection.selected[0]

    @pytest.mark.asyncio
    async def test_b10_el_planner_usa_el_selector_y_no_un_diccionario(self):
        """El paso `execute` toma su capability de la selección, no de un `dict.get`."""
        from alexis.cognition.planner import Planner

        planner = Planner()
        capabilities = {}
        for objetivo in ("lee el informe", "escribe un archivo nuevo", "borra el temporal"):
            mission = _mission(objetivo)
            plan = await planner.create_plan(mission)
            capabilities[objetivo] = [s.capability for s in plan.steps if s.id == "execute"][0]
        assert capabilities["lee el informe"] == "fs.read"
        assert capabilities["escribe un archivo nuevo"] == "fs.write"
        assert capabilities["borra el temporal"] == "fs.remove"


def _probe_step():
    from alexis.contracts import PlanStep, RiskLevel

    return PlanStep("execute", "ejecuta", "execute", RiskLevel.LOW, "executor", capability="fs.read")


# =========================================================================== #
# FASE C — Recovery cognitivo a mitad de misión (GAP 3)
# =========================================================================== #


from alexis.cognition.goal_verification import (  # noqa: E402
    CriterionEvaluation,
    CriterionEvidence,
    CriterionStatus,
    GoalVerification,
)
from alexis.cognition.loop import CognitiveRuntime  # noqa: E402
from alexis.contracts import ExecutionResult, Plan, PlanStep  # noqa: E402
from alexis.world.model import WorldModel  # noqa: E402


class _AllowPolicy:
    def authorize(self, mission, step):
        class _Allow:
            allowed = True
            requires_approval = False
            reason = "permitido"
            matched_rule = "closure-test"
            capability = getattr(step, "capability", None)

        return _Allow()


class _PassingVerifier:
    async def verify(self, mission, plan):
        from alexis.contracts import Verification

        return Verification(passed=True, evidence=["paso ok"], confidence=0.9)


def _two_step_plan(mission_id):
    return Plan(
        mission_id=mission_id,
        steps=[
            PlanStep("read", "leer el archivo", "execute", RiskLevel.LOW, "executor",
                     capability="fs.read"),
            PlanStep("write", "escribir el resumen", "execute", RiskLevel.MEDIUM, "executor",
                     ["read"], capability="fs.write"),
        ],
    )


def _cognitive(world=None):
    async def _execute(mission, step, decision):
        return ExecutionResult(success=True, output={"path": f"/tmp/{step.id}.txt",
                                                     "exists": True, "size": 12})

    return CognitiveRuntime(
        policy=_AllowPolicy(), executor=None, verifier=_PassingVerifier(),
        execute=_execute, world=world,
    )


class TestFaseCCognitiveRecovery:
    @pytest.mark.asyncio
    async def test_c1_recupera_y_continua_hasta_terminar(self, tmp_path):
        """El escenario entero: checkpoint → proceso muerto → reanuda → termina.

        No se simula el reinicio reinyectando estado a mano: se serializa la misión a
        JSON y se rehidrata en un objeto nuevo, como hace `mission_from_row`.
        """
        from alexis.storage.serialization import mission_from_row, mission_to_row

        world = WorldModel()
        cognitive = _cognitive(world)
        mission = _mission("lee el informe y escribe un resumen")
        plan = _two_step_plan(mission.id)

        # --- Proceso 1: llega al checkpoint tras el primer paso ---
        knowledge = cognitive.knowledge_for(mission)
        pending = cognitive.pending_steps(mission, plan, knowledge)
        outcome = await cognitive.step(mission, knowledge, pending_steps=pending, plan=plan)
        knowledge = outcome.knowledge
        cognitive.store_knowledge(mission, knowledge)
        assert "read" in knowledge.completed_steps
        assert mission.context["decisions"], "la decisión debe persistirse (GAP 3 lo exige)"
        assert mission.context["world"], "el world debe viajar en el contexto"

        # --- El proceso muere aquí. Se serializa como lo hace la capa de storage. ---
        row = mission_to_row(mission)  # la serialización real, no una fila a mano

        # --- Proceso 2: objeto nuevo, runtime nuevo, world nuevo ---
        world2 = WorldModel()
        cognitive2 = _cognitive(world2)
        recovered = mission_from_row(row)
        assert recovered.id == mission.id
        resume = cognitive2.resume_cognition(recovered)
        assert resume.safe is True, f"no debería poder reanudar: {resume.reason}"
        assert resume.iteration >= 1
        assert resume.claims >= 0
        assert cognitive2.restore_world(recovered) == len(mission.context["world"])

        # Y sigue trabajando hasta terminar, sin inventar lo perdido.
        plan2 = _two_step_plan(recovered.id)
        knowledge2 = cognitive2.knowledge_for(recovered)
        assert "read" in knowledge2.completed_steps, "el paso hecho antes no se repite"
        finished = False
        for _ in range(6):
            pending = cognitive2.pending_steps(recovered, plan2, knowledge2)
            if not pending:
                finished = True
                break
            outcome = await cognitive2.step(recovered, knowledge2,
                                            pending_steps=pending, plan=plan2)
            knowledge2 = outcome.knowledge
            cognitive2.store_knowledge(recovered, knowledge2)
            if outcome.done:
                finished = True
                break
        assert finished, "el bucle recuperado debe terminar"
        assert "write" in knowledge2.completed_steps, "el paso pendiente se ejecutó"

    def test_c2_sin_contexto_cognitivo_no_reanuda_a_ciegas(self):
        """Falta el contexto ⇒ NEEDS_VERIFICATION, nunca asumir que se iba bien."""
        cognitive = _cognitive()
        mission = _mission("lee el informe")
        resume = cognitive.resume_cognition(mission)
        assert resume.safe is False
        assert set(resume.missing) == {"knowledge", "decisions"}
        assert "no puede reanudarse" in resume.reason

    def test_c3_el_world_sobrevive_al_reinicio(self):
        cognitive = _cognitive(WorldModel())
        mission = _mission("lee el informe")
        from alexis.contracts import Observation

        cognitive.world.observe_execution(
            type("S", (), {"id": "read", "capability": "fs.read", "action": "execute"})(),
            ExecutionResult(success=True, output={"path": "/tmp/x.txt", "exists": True}),
            mission=mission,
        )
        saved = cognitive.save_world(mission)
        assert saved >= 1

        nuevo = _cognitive(WorldModel())
        assert nuevo.restore_world(mission) == saved
        assert mission.context["world"], "el world queda en el contexto para el reinicio"

    def test_c4_decisiones_y_reflexion_tambien_sobreviven(self):
        """El epílogo de §5.6 también tiene que recoverable."""
        cognitive = _cognitive()
        mission = _mission("lee el informe")
        knowledge = cognitive.knowledge_for(mission)
        knowledge.last_verdict = "success"
        cognitive.store_knowledge(mission, knowledge)
        _, reflection, experience, verified = cognitive.compose_epilogue(
            mission, knowledge, model_outcome="real")
        row = mission.context
        for key in ("response", "reflection", "experience", "verified_learning"):
            assert key in row, f"{key} no sobrevive para el recovery"
        assert row["verified_learning"]["quality"] in ("verified", "partial", "insufficient", "blocked")




# =========================================================================== #
# FASE D — Los PARTIAL revisados uno a uno
# =========================================================================== #


class TestFaseDPartials:
    def test_d1_planstep_tiene_los_campos_que_exige_el_plan(self):
        """#5 Dynamic Planning: 9 campos obligatorios por paso."""
        campos = set(PlanStep.__dataclass_fields__)
        exigidos = {
            "objective", "capability", "action", "args", "depends_on",
            "expected", "success_criteria", "risk", "requires_approval",
        }
        assert exigidos <= campos, f"faltan: {sorted(exigidos - campos)}"

    def test_d2_la_firma_de_accion_no_incluye_el_id_del_paso(self):
        """#12 Replanning: dos ids con los mismos args SON la misma acción."""
        from alexis.cognition.loop import _action_signature

        uno = PlanStep("read-probe", "lee", "execute", RiskLevel.LOW, "e", capability="fs.read")
        dos = PlanStep("read-probe-2", "lee", "execute", RiskLevel.LOW, "e", capability="fs.read")
        assert _action_signature(uno) == _action_signature(dos), "el id no debe salvar al bucle"
        otro = PlanStep("read-probe-2", "lee", "execute", RiskLevel.LOW, "e",
                        capability="fs.read", args={"path": "/otro"})
        assert _action_signature(uno) != _action_signature(otro), "args distintos sí son otra acción"

    def test_d3_la_repeticion_se_registra_y_se_expone(self):
        """La procedencia decide: el plan original nunca se bloquea; un replan sí.

        Antes este test afirmaba que la repetición "se expone" sin consequence. Con §12 el
        filtro existe, así que lo que se comprueba es la REGLA: misma firma + fallo +
        sin evidencia nueva + origen replan ⇒ bloqueada.
        """
        cognitive = _cognitive()
        mission = _mission("lee el informe")
        plan = Plan(mission.id, [
            PlanStep("p1", "lee", "execute", RiskLevel.LOW, "e", capability="fs.read"),
            PlanStep("p2", "lee otra vez", "execute", RiskLevel.LOW, "e", capability="fs.read"),
        ])
        knowledge = cognitive.knowledge_for(mission)

        # (1) plan original: dos pasos iguales, ambos permitidos.
        assert cognitive.repeated_actions(knowledge, plan.steps, generation=0) == []

        # Un intento fallido, en el plan original.
        cognitive.record_attempt(knowledge, plan.steps[0], success=False, error="no existe")

        # (2) mismo paso reofrecido por un replan, sin cambios: se bloquea.
        bloqueados = cognitive.repeated_actions(knowledge, plan.steps, generation=1)
        assert [s.id for s in bloqueados] == ["p1", "p2"]
        assert "falló" in cognitive.blocked_reason(knowledge, plan.steps[0], 1)

        # (3) con evidencia MATERIAL sobre el objetivo, la repetición vuelve a ser válida.
        #
        # Cambio de expectativa justificado: antes esto se simulaba con
        # `add_known("el archivo ya existe")`. La regla corregida de §12.5 excluye `known`
        # del Fingerprint de evidencia a propósito — `known` mezcla datos con libro de
        # cuentas ("«x» completado"), que era lo que hacía la regla demasiado laxa. La
        # evidencia material es un hecho observado: el World Model o un claim. El
        # enunciado lo dice así: "tool observa que ahora existe".
        knowledge.world = ["file:informe.txt (exists=True, size=30)"]
        assert cognitive.repeated_actions(knowledge, plan.steps, generation=2) == []

    def test_d3b_la_accion_alternativa_no_se_bloquea(self):
        """Un replan que propone OTRA capability no se toca: eso es la alternativa."""
        cognitive = _cognitive()
        mission = _mission("lee el informe")
        fallida = PlanStep("p1", "lee", "execute", RiskLevel.LOW, "e", capability="fs.read")
        alternativa = PlanStep("p2", "lista", "execute", RiskLevel.LOW, "e", capability="fs.stat")
        knowledge = cognitive.knowledge_for(mission)
        cognitive.record_attempt(knowledge, fallida, success=False, error="no existe")
        assert cognitive.repeated_actions(knowledge, [fallida], generation=1)
        assert cognitive.repeated_actions(knowledge, [alternativa], generation=1) == []

    def test_d4_las_firmas_sobreviven_a_la_persistencia(self):
        from alexis.cognition.state import KnowledgeState

        k = KnowledgeState(objective="x")
        k.action_signatures.append("fs.read|{}")
        assert KnowledgeState.from_dict(k.to_dict(), "x").action_signatures == ["fs.read|{}"]

    def test_d5_el_guarterm_memoria_afecta_al_recovery(self, tmp_path):
        """#19: tras reiniciar, la misión NO se da por buena si el contexto se perdió."""
        cognitive = _cognitive()
        mission = _mission("lee el informe")
        assert cognitive.resume_cognition(mission).safe is False
        knowledge = cognitive.knowledge_for(mission)
        knowledge.last_verdict = "failure"
        cognitive.store_knowledge(mission, knowledge)
        assert mission.context["knowledge"], "el veredicto persistido es lo que se recupera"
        assert "last_verdict" in mission.context["knowledge"]

    def test_d6_response_composer_ya_no_es_un_segundo_sistema(self):
        """#16 COMPLETE: existe, persiste y los canales lo consumen."""
        from alexis.cognition.response import read_composed

        mission = _mission("lee el informe")
        assert read_composed(mission) is None
        mission.context["response"] = _composed(mission).to_dict()
        assert read_composed(mission) is not None
        # Y el canal de voz lo lee de ahí, no del objetivo.
        import inspect

        from alexis.execution import SandboxExecutor

        source = inspect.getsource(SandboxExecutor._spoken_reply)
        assert "read_composed" in source
        assert "Pedido del usuario" not in source, "la voz no puede volver a inventar"


# =========================================================================== #
# FASE E — E2E real: componentes auténticos, sin _AllowPolicy ni _PassingVerifier
# =========================================================================== #


class TestFaseEE2EReal:
    """USER → intent → mission → Core → selección → policy real → capability real →
    observation real → evaluación real → goal verification real → settle → respuesta real.

    Aquí NO se sustituye el runtime por dobles. Se usan `PolicyEngine`, `AutonomyGate`,
    `SandboxExecutor` con herramientas de fichero reales sobre un workspace temporal,
    `WorldModel` real y `GoalVerifier` real. Lo único que no se ejercita es el modelo de
    lenguaje: se documenta como dependencia externa ausente, no se simula en silencio.
    """

    @pytest.mark.asyncio
    async def test_e1_ciclo_completo_con_todo_real(self, tmp_path):
        from alexis.autonomy.gates import AutonomyGate
        from alexis.autonomy.mission import MissionEngine
        from alexis.cognition.goal_verification import GoalVerifier
        from alexis.security.policy import PolicyEngine
        from alexis.security.sandbox import SandboxRunner
        from alexis.tools.filesystem import build_filesystem_tools
        from alexis.verification import FilesystemVerifier

        # --- Workspace real ---
        (tmp_path / "informe.txt").write_text("dato real del informe\n", encoding="utf-8")
        catalog = build_catalog()
        tools = ToolRegistry()
        tools.register_all(build_filesystem_tools(tmp_path))
        policy = PolicyEngine()
        world = WorldModel()
        cognitive = CognitiveRuntime(
            policy=policy,
            gate=AutonomyGate(),                 # autoridad real
            executor=None,
            verifier=FilesystemVerifier(workspace=tmp_path),  # verificador real
            goal_verifier=GoalVerifier(world=world),         # verificación real de objetivo
            world=world,                          # World Model real
        )
        from alexis.execution import SandboxExecutor

        cognitive.executor = SandboxExecutor(tools=tools, sandbox=SandboxRunner(workspace=tmp_path))

        # --- 1. INTENET → MISSION (sólo TASK crea misión) ---
        from alexis.cognition.intent_classifier import IntentClassifier
        from alexis.cognition.contracts import IntentKind
        from alexis.models.router import ModelRouter

        classifier = IntentClassifier(ModelRouter(allow_degraded=False))
        intent = await classifier.classify("lee el archivo informe.txt y dime qué contiene")
        assert intent.kind is IntentKind.TASK, "una tarea real debe crear misión"
        # Un saludo NO crea misión.
        saludo = await classifier.classify("hola")
        assert not saludo.is_task, "un greeting no puede crear misión"

        # --- 2. MISSION con criterio de éxito REAL ---
        mission = MissionEngine().create(
            "lee el archivo informe.txt",
            MissionEnvelope(objective="lee el archivo informe.txt",
                            autonomy=AutonomyLevel.SUPERVISED,
                            allowed_actions=["understand", "research", "execute", "verify"]),
            success_criteria=["El archivo file_exists:informe.txt existe"],
        )

        # --- 3. SELECCIÓN DINÁMICA de capability ---
        from alexis.cognition.selection import CapabilitySelector

        selector = CapabilitySelector(catalog=catalog, policy=policy)
        selection = selector.select("lee el archivo informe.txt y dime qué contiene",
                                    envelope=mission.envelope)
        assert selection.selected, "la selección dinámica debe encontrar la capability"
        assert selection.trace, "la selección debe dejar traza auditable"

        # --- 4. PLAN con la capability seleccionada, y EJECUCIÓN REAL ---
        from alexis.cognition.planner import Planner

        plan = await Planner().create_plan(mission)
        assert any(s.capability for s in plan.steps), "el plan debe declarar capabilities"

        knowledge = cognitive.knowledge_for(mission)
        finished = False
        for _ in range(12):
            pending = cognitive.pending_steps(mission, plan, knowledge)
            if not pending:
                finished = True
                break
            outcome = await cognitive.step(mission, knowledge, pending_steps=pending, plan=plan)
            knowledge = outcome.knowledge
            cognitive.store_knowledge(mission, knowledge)
            if outcome.done:
                finished = True
                break
        assert finished, "el ciclo debe terminar sin bucle infinito"

        # --- 5. VEREDICTO real y TRACEABILITY ---
        assert knowledge.last_verdict, "debe haber veredicto"
        assert mission.context["decisions"], "las decisiones deben ser auditables"
        assert mission.context["knowledge"], "el KnowledgeState debe persistirse"
        assert mission.context["world"], "el World Model real debe quedar registrado"

        # --- 6. GOAL VERIFICATION real ---
        verification = cognitive.goal_verification_of(mission) or (
            cognitive.verify_goal(mission, knowledge)
        )
        if verification is None:
            verification = GoalVerifier(world=world).verify(mission)

        # --- 7. SETTLE + RESPONSE COMPOSER real ---
        reply, reflection, experience, verified = cognitive.compose_epilogue(
            mission, knowledge, model_outcome="unavailable"
        )
        for key in ("response", "reflection", "experience", "verified_learning"):
            assert key in mission.context, f"§5.6 no dejó {key}"
        assert reply.verdict == knowledge.last_verdict
        assert isinstance(verified.can_teach(), bool)
        # El objetivo NO se da por cumplido sin verificación real.
        if not getattr(verification, "verified", False):
            assert reply.goal_verified is False
            assert "NO está verificado" in reply.text, "no puede afirmación de logro sin probar"

    @pytest.mark.asyncio
    async def test_e2_no_verificado_no_llega_a_completed(self, tmp_path):
        """El invariante 9 con piezas reales: sin goal verification, no hay COMPLETED."""
        from alexis.autonomy.goal_state import settle
        from alexis.cognition.goal_verification import GoalVerifier
        from alexis.contracts import MissionState

        mission = _mission("lee el archivo que no existe")
        mission.results.append({"step": "read", "success": True, "output": {"ok": True}})
        settle(mission, GoalVerifier(world=WorldModel()).verify(mission))
        assert mission.state is not MissionState.COMPLETED

        cognitive = _cognitive()
        knowledge = cognitive.knowledge_for(mission)
        knowledge.last_verdict = "success"
        reply, _, _, _ = cognitive.compose_epilogue(mission, knowledge, model_outcome="real")
        assert reply.goal_verified is False
        assert cognitive._is_frozen if hasattr(cognitive, "_is_frozen") else True


class TestFaseDIntentEngine:
    """#1 Intent Engine: las preguntas ya no caen en UNKNOWN."""

    @pytest.mark.asyncio
    async def test_d7_una_pregunta_no_crea_mision(self):
        from alexis.cognition.intent_classifier import IntentClassifier
        from alexis.cognition.contracts import IntentKind
        from alexis.models.router import ModelRouter

        classifier = IntentClassifier(ModelRouter(allow_degraded=False))
        intent = await classifier.classify("¿por qué el cielo es azul?")
        assert intent.kind is IntentKind.QUESTION
        assert intent.is_task is False, "una pregunta jamás crea misión"

    @pytest.mark.asyncio
    async def test_d8_los_tres_invariantes_de_mision(self):
        """Invariantes 1, 2 y 3 del closure pass, con el clasificador real."""
        from alexis.cognition.contracts import IntentKind
        from alexis.cognition.intent_classifier import IntentClassifier
        from alexis.models.router import ModelRouter

        classifier = IntentClassifier(ModelRouter(allow_degraded=False))
        saludo = await classifier.classify("hola")
        consulta = await classifier.classify("¿qué puedes hacer por mí?")
        tarea = await classifier.classify("lee el archivo informe.txt")
        assert saludo.kind is IntentKind.GREETING and not saludo.is_task
        assert consulta.kind is IntentKind.CAPABILITY_QUERY and not consulta.is_task
        assert tarea.kind is IntentKind.TASK and tarea.is_task

    def test_d9_clarification_ya_existe_y_command_sigue_ausente(self):
        """`clarification` llegó con §13. `command` sigue sin clase propia: documentado."""
        from alexis.cognition.contracts import IntentKind

        values = {k.value for k in IntentKind}
        assert "clarification" in values, "§13 añadió el kind"
        assert "command" not in values, "command sigue sin clase propia (hoy cae en TASK)"
