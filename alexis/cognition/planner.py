from alexis.contracts import Plan, PlanStep, RiskLevel
from alexis.capabilities import ACTION_TO_CAPABILITY
from alexis.perception.activation import is_activation_objective
from alexis.tools.desktop import desktop_tool_for
from alexis.tools.filesystem import (
    classify_objective_intent,
    extract_workspace_path,
    is_informational_objective,
)

#: Marcas de procedencia por paso (Req 6): distinguen la estrategia GENERADA para el
#: objetivo de la plantilla universal declarada como SÓLO fallback. Son solo traza:
#: la autoridad seguirá siendo Policy/Gate/PlanValidator/GoalVerifier.
PLANNED_OBJECTIVE_DRIVEN = "objective_driven"
PLANNED_FALLBACK_TEMPLATE = "fallback"

#: Verbos que piden síntesis/análisis: el plan reúne evidencia y termina analizándola
#: (vocabulario ejecutable `research` → `analyze`), en vez de tocar el workspace.
_SYNTHESIS_OBJECTIVE_HINTS = (
    "analiza", "analizar", "análisis", "explica", "explicar", "explícame",
    "compara", "comparar", "calcula", "resume", "resumen", "investiga", "revisa",
)

#: Objetivos que preguntan por EXISTENCIA/estado: empiezan con una observación `fs.stat`
#: y se cierran con una verificación independiente, sin tocar nada.
_EXISTENCE_OBJECTIVE_HINTS = ("existe", "existe el", "hay un archivo", "haya un archivo")

#: Capability preferida por familia, tomada del catálogo REAL (no inventada).
_FAMILY_PREFERRED = {
    "write": ("fs.write",),
    "remove": ("fs.remove",),
    "read": ("fs.read", "research.filesystem", "fs.stat"),
    "analyze": ("fs.read", "fs.stat", "research.filesystem"),
    "existence": ("fs.stat", "research.filesystem"),
    "transform": ("fs.read", "fs.write"),
}

#: P0 §11 — un objetivo TRANSFORMACIONAL nombra dos rutas: una de la que se parte y otra
#: que se produce. "crea salida.txt con un resumen de notas.txt" no es una escritura de
#: `salida.txt`: es leer `notas.txt` y escribir `salida.txt`. Sin esta detección el plan
#: era `[execute, verify]` y la entrada no se leía nunca, de modo que la cadena causal que
#: el objetivo pide no existía —el sistema escribía un fichero cuyo contenido no procedía
#: de nada observado—.
_TRANSFORM_HINTS = (
    "a partir de", "basado en", "basada en", "con un resumen de", "con la resumen de",
    "con una copia de", "copia de", "traduce", "traducir", "transforma", "transformar",
)


def _transform_paths(objective: str) -> tuple[str, str]:
    """(entrada, salida) de un objetivo transformacional, o `("", "")` si no lo es.

    Reutiliza la clasificación semántica de `criteria.py`: si el contrato de la misión
    exige `content_observed` de una ruta y `file_exists` de otra, el plan tiene que hacer
    exactamente eso. Una sola fuente de verdad para decidir qué es entrada y qué salida.
    """
    from alexis.cognition.criteria import classify_objective_semantics, criteria_for_objective

    if classify_objective_semantics(objective) != "transformation":
        return "", ""
    criteria = criteria_for_objective(objective)
    source = ""
    target = ""
    for criterion in criteria:
        name, _, argument = criterion.partition(":")
        argument = argument.split(":")[0]
        if name == "content_observed" and argument:
            source = argument
        elif name in ("file_exists", "file_size_at_least") and argument:
            target = argument
    if source and target and source != target:
        return source, target
    return "", ""


def _objective_family(objective: str, intent: str) -> str:
    """Familia de FORMA del plan, derivada de la intención y de señales léxicas.

    `transform` gana a `write`: si el objetivo tiene entrada y salida, el plan tiene que
    abarcar ambos. Un objetivo que empieza por un verbo de creación no es una
    escritura aislada cuando además nombra de dónde sale lo que hay que escribir.

    Los verbos de síntesis ganan a la lectura (el objetivo pide conclusiones, no solo
    leer). `unsupported` devuelve la plantilla declarada como fallback.
    """
    folded = (objective or "").strip().lower()
    if intent == "unsupported":
        return "unsupported"
    source, target = _transform_paths(objective or "")
    if source and target:
        return "transform"
    if intent == "write":
        return "write"
    if intent == "destructive":
        return "remove"
    if any(h in folded for h in _SYNTHESIS_OBJECTIVE_HINTS):
        return "analyze"
    if any(h in folded for h in _EXISTENCE_OBJECTIVE_HINTS):
        return "existence"
    return "read"


def _pick_capability(selected, catalog, preferred) -> str | None:
    """Primera capability preferida que el selector propuso y el catálogo habilita."""
    for capability in preferred:
        if capability in selected and catalog.is_enabled(capability):
            return capability
    return None


class Planner:
    """Planner por capacidades (F1).

    Cada paso declara la `capability` que necesita (`fs.read`, `fs.write`…). Requisito 6:
    el DAG de etapas se genera SEGÚN el objetivo (lectura observa y verifica según el
    caso; escritura aplica el efecto mínimo y verifica; borrado pide aprobación y
    verifica; análisis reúne evidencia y la analiza) usando el catálogo REAL y el
    `CapabilitySelector` para elegir la capability. La plantilla universal por etapas
    queda SOLO como fallback declarado (intención no soportada o sin catálogo). Los ids
    de los pasos generados son estables para no romper checkpoint/resume/approval.
    """

    @staticmethod
    def plan_from_skill(mission, skill_version) -> Plan | None:
        """CORE-12 — plan desde el `procedure` de una skill validada, o `None`.

        No añade ni quita nada: la skill ES la estrategia. Cada paso conserva su id (para
        checkpoint/resume), capability, acción, riesgo, args y requires_approval, y se marca
        `proposed_by="skill"` para que la traza distinga un plan reutilizado de una plantilla.
        Una skill con estructura corrupta nunca puede tumbar la planificación.
        """
        try:
            procedure = list(getattr(skill_version, "procedure", []) or [])
            if not procedure:
                return None
            steps: list[PlanStep] = []
            for index, raw in enumerate(procedure):
                capability = raw.get("capability")
                action = raw.get("action", "execute")
                if not capability:
                    return None
                try:
                    risk = RiskLevel(raw.get("risk", "low"))
                except ValueError:
                    risk = RiskLevel.LOW
                steps.append(
                    PlanStep(
                        id=raw.get("id") or f"skill-step-{index}",
                        description=raw.get(
                            "description",
                            f"Skill {skill_version.name} v{skill_version.version}: {action} {capability}",
                        ),
                        action=action,
                        risk=risk,
                        agent=raw.get("agent", "executor"),
                        depends_on=list(raw.get("depends_on") or []),
                        requires_approval=bool(raw.get("requires_approval", False)),
                        capability=capability,
                        proposed_by="skill",
                        rationale=f"procedimiento reutilizado de la skill {skill_version.name}",
                        args=dict(raw.get("args") or {}),
                    )
                )
            return plan_with_contract(mission, steps)
        except Exception:  # noqa: BLE001 — una skill corrupta nunca tumbar planificar
            return None

    def _select_execute_capability(self, mission, objective: str, intent: str):
        """Capability de ejecución elegida por selección dinámica, o `None` si no se puede.

        Se consulta al catálogo REAL. Si no hay catálogo (tests legacy, entornos sin
        capabilities registradas), devuelve `None` y quien llama usa el respaldo.
        """
        from alexis.capabilities.catalog import build_catalog
        from alexis.cognition.selection import CapabilitySelector

        try:
            catalog = build_catalog()
        except Exception:  # noqa: BLE001 — sin catálogo no hay selección que hacer
            return None
        if not catalog.enabled():
            return None
        selector = CapabilitySelector(catalog=catalog)
        selection = selector.best(objective, envelope=getattr(mission, "envelope", None))
        if not selection.selected:
            # Nada seleccionable: `decide_unavailable` dice si preguntar, replanear o abortar.
            selector.decide_unavailable(selection)
            return None
        return selection.selected[0]

    async def create_plan(self, mission) -> Plan:
        objective = mission.goal.objective
        if is_activation_objective(objective):
            return plan_with_contract(mission, [
                PlanStep("understand", "Understand the activation request", "analyze", RiskLevel.LOW, "reasoner",
                         capability="cognition.understand"),
                PlanStep("respond", "Greet the user and ask for instructions", "respond", RiskLevel.LOW, "responder",
                         ["understand"], capability="tts.speak"),
            ])

        desktop = desktop_tool_for(objective)
        if desktop is not None:
            tool_name = desktop[0]
            return plan_with_contract(mission, [
                PlanStep("understand", f"Understand objective: {objective}", "analyze", RiskLevel.LOW, "reasoner",
                         capability="cognition.understand"),
                PlanStep("execute", f"Dispatch desktop tool {tool_name}", "execute", RiskLevel.MEDIUM,
                         "executor", ["understand"], capability="desktop.tools"),
                PlanStep("respond", "Confirm the desktop action", "respond", RiskLevel.LOW, "responder",
                         ["execute"], capability="tts.speak"),
            ])

        intent = classify_objective_intent(objective)
        if is_informational_objective(objective):
            return plan_with_contract(mission, [
                PlanStep("understand", f"Understand the question: {objective}", "analyze", RiskLevel.LOW,
                         "reasoner", capability="cognition.understand"),
                PlanStep("respond", "Answer conversationally in Spanish", "respond", RiskLevel.LOW,
                         "responder", ["understand"], capability="tts.speak"),
            ])

        # Requisito 6: primero la forma objetiva del plan (DAG según objetivo y catálogo).
        objective_plan = self._plan_objective_driven(mission, objective, intent)
        if objective_plan is not None:
            return plan_with_contract(mission, objective_plan)

        # Plantilla universal SÓLO como fallback declarado: intención no soportada o sin
        # catálogo/selección disponible. La capability se elige con `CapabilitySelector`
        # cuando hay catálogo; el `dict` queda únicamente como respaldo legacy.
        return plan_with_contract(mission, self._fallback_template(mission, objective, intent))

    def _plan_objective_driven(self, mission, objective: str, intent: str) -> list[PlanStep] | None:
        """DAG específico del objetivo (Req 6), o `None` para declarar el fallback.

        La capability se elige contra el catálogo REAL y la propuesta del
        `CapabilitySelector` (quien SELECCIONA, nunca autoriza). Los pasos se limitan al
        vocabulario ejecutable (`research`/`execute`/`verify`/`analyze`), y cada uno
        declara su objective/success_criteria vía `plan_with_contract`.
        """
        from alexis.capabilities.catalog import build_catalog
        from alexis.cognition.selection import CapabilitySelector

        try:
            catalog = build_catalog()
        except Exception:  # noqa: BLE001 — sin catálogo no hay forma objetiva que garantizar
            return None
        if catalog is None or not catalog.enabled():
            return None

        family = _objective_family(objective, intent)
        preferred = _FAMILY_PREFERRED.get(family)
        if preferred is None:
            return None

        # P0 §11: en una transformación, entrada y salida se calculan aquí y se usan
        # abajo para encadenar los pasos. Se recalcula en lugar de confiar en el objetivo
        # pelado porque es la MISMA función que decidió la familia: una sola verdad.
        source, target_path = _transform_paths(objective or "")

        try:
            selector = CapabilitySelector(catalog=catalog)
            selection = selector.select(objective, envelope=getattr(mission, "envelope", None))
        except Exception:  # noqa: BLE001 — la selección nunca tumba la planificación
            return None
        capability = _pick_capability(list(selection.selected), catalog, preferred)
        if capability is None:
            capability = next(
                (c for c in preferred if catalog.has(c) and catalog.is_enabled(c)),
                None,
            )
        if capability is None:
            return None

        target = extract_workspace_path(objective) or None
        args = {"path": target} if target else {}

        if family == "transform":
            # P0 §11 — el plan tiene que ENCADENAR la transformación: primero se observa
            # la entrada, después se produce la salida, y sólo entonces se verifica de
            # forma independiente. El paso de lectura declara `depends_on` vacío y el de
            # escritura depende de él: no se puede "resumir" un fichero que no se ha leído,
            # y el plan lo hace imposible en lugar de confiar en que el modelo lo haga.
            read_capability = (
                _pick_capability(list(selection.selected), catalog, ("fs.read", "research.filesystem"))
                or "fs.read"
            )
            write_capability = (
                _pick_capability(list(selection.selected), catalog, ("fs.write",))
                or "fs.write"
            )
            return [
                PlanStep(
                    "research",
                    f"Leer {source} para poder trabajar sobre su contenido",
                    "research",
                    RiskLevel.LOW,
                    "researcher",
                    capability=read_capability,
                    proposed_by=PLANNED_OBJECTIVE_DRIVEN,
                    rationale=(
                        "P0 §11: la salida que se pide deriva de esta entrada; sin "
                        "observarla, escribir el fichero sería inventar su contenido"
                    ),
                    args={"path": source},
                ),
                PlanStep(
                    "execute",
                    f"Escribir {target} a partir del contenido observado",
                    "execute",
                    RiskLevel(catalog.get(write_capability).default_risk)
                    if catalog.has(write_capability)
                    else RiskLevel.MEDIUM,
                    "executor",
                    ["research"],
                    capability=write_capability,
                    proposed_by=PLANNED_OBJECTIVE_DRIVEN,
                    rationale="P0 §11: la producción depende de la lectura previa",
                    args={"path": target_path},
                ),
                PlanStep(
                    "verify",
                    f"Verificar de forma independiente que {target_path} quedó como se pidió",
                    "verify",
                    RiskLevel.LOW,
                    "critic",
                    ["execute"],
                    capability="verification.filesystem",
                    proposed_by=PLANNED_OBJECTIVE_DRIVEN,
                    rationale="Req 6: verificación read-only e independiente de la acción",
                ),
            ]
        if family == "write" or family == "remove":
            effect = (
                RiskLevel(catalog.get(capability).default_risk)
                if catalog.has(capability)
                else RiskLevel.MEDIUM
            )
            return [
                PlanStep(
                    "execute",
                    f"{'Eliminar' if family == 'remove' else 'Escribir'} "
                    f"{target or 'el objetivo'}",
                    "execute",
                    effect,
                    "executor",
                    requires_approval=family == "remove",
                    capability=capability,
                    proposed_by=PLANNED_OBJECTIVE_DRIVEN,
                    rationale=(
                        "Req 6: el efecto mínimo útil que resuelve el objetivo y su "
                        "verificación independiente"
                    ),
                    args=args,
                ),
                PlanStep(
                    "verify",
                    "Verificar de forma independiente el efecto producido",
                    "verify",
                    RiskLevel.LOW,
                    "critic",
                    ["execute"],
                    capability="verification.filesystem",
                    proposed_by=PLANNED_OBJECTIVE_DRIVEN,
                    rationale=(
                        "Req 6: verificación read-only e independiente de la acción"
                    ),
                ),
            ]
        if family == "analyze":
            return [
                PlanStep(
                    "research",
                    f"Reunir la evidencia del dominio del objetivo",
                    "research",
                    RiskLevel.LOW,
                    "researcher",
                    capability=capability,
                    proposed_by=PLANNED_OBJECTIVE_DRIVEN,
                    rationale="Req 6: observar antes de concluir",
                    args=args,
                ),
                PlanStep(
                    "analyze",
                    "Sintetizar conclusiones a partir de la evidencia reunida",
                    "analyze",
                    RiskLevel.LOW,
                    "reasoner",
                    ["research"],
                    capability="cognition.analyze",
                    proposed_by=PLANNED_OBJECTIVE_DRIVEN,
                    rationale="Req 6: la conclusión se construye sobre la evidencia",
                ),
            ]
        # Lectura / existencia: sólo observar. Si el objetivo pregunta por estado, la
        # observación es `fs.stat` y se cierra con verificación independiente.
        steps = [
            PlanStep(
                "research",
                "Obtener una observación del dominio del objetivo",
                "research",
                RiskLevel.LOW,
                "researcher",
                capability="fs.stat" if family == "existence" else capability,
                proposed_by=PLANNED_OBJECTIVE_DRIVEN,
                rationale="Req 6: la lectura no modifica el workspace",
                args=args,
            )
        ]
        if family == "existence":
            steps.append(
                PlanStep(
                    "verify",
                    "Verificar de forma independiente el estado observado",
                    "verify",
                    RiskLevel.LOW,
                    "critic",
                    ["research"],
                    capability="verification.filesystem",
                    proposed_by=PLANNED_OBJECTIVE_DRIVEN,
                    rationale="Req 6: el estado se confirma con una observación aparte",
                )
            )
        return steps

    def _fallback_template(self, mission, objective: str, intent: str) -> list[PlanStep]:
        """Plantilla universal por etapas, SÓLO como fallback declarado (Req 6)."""
        delicate = intent == "destructive"
        # P0 §5.6 / requisito 6: la capability NO se elige con un `dict.get` sobre la
        # intención. La decide `CapabilitySelector` contra el catálogo real, el envelope y
        # la policy. El `dict` queda sólo como respaldo cuando no hay catálogo disponible.
        execute_capability = self._select_execute_capability(mission, objective, intent)
        if execute_capability is None:
            execute_capability = {
                "write": "fs.write",
                "destructive": "fs.remove",
                "unsupported": "execution.sandbox",
            }.get(intent, "fs.read")
        return [
            PlanStep("understand", f"Understand objective: {objective}", "analyze", RiskLevel.LOW, "reasoner",
                     capability="cognition.understand", proposed_by=PLANNED_FALLBACK_TEMPLATE),
            PlanStep("research", "Gather relevant evidence and context", "research", RiskLevel.LOW, "researcher",
                     ["understand"], capability="research.filesystem", proposed_by=PLANNED_FALLBACK_TEMPLATE),
            PlanStep(
                "execute",
                "Execute the smallest useful action",
                "execute",
                RiskLevel.MEDIUM,
                "executor",
                ["research"],
                requires_approval=delicate,
                capability=execute_capability,
                proposed_by=PLANNED_FALLBACK_TEMPLATE,
            ),
            PlanStep("verify", "Independently verify the result", "verify", RiskLevel.LOW, "critic",
                     ["execute"], capability="verification.filesystem", proposed_by=PLANNED_FALLBACK_TEMPLATE),
        ]


def _step_capability(step) -> str | None:
    """Capability del paso: explícita del plan, o mapeada por acción si viene de obra
    legada (persistencia antigua)."""
    cap = getattr(step, "capability", None)
    if cap:
        return cap
    return ACTION_TO_CAPABILITY.get(getattr(step, "action", ""))


def plan_to_dict(plan: Plan) -> list[dict]:
    return [
        {
            "id": s.id,
            "description": s.description,
            "action": s.action,
            "risk": s.risk.value,
            "agent": s.agent,
            "depends_on": list(s.depends_on),
            "requires_approval": s.requires_approval,
            "capability": _step_capability(s),
            "requires_input": dict(getattr(s, "requires_input", {}) or {}),
            "verification": getattr(s, "verification", None),
            "proposed_by": getattr(s, "proposed_by", None),
            "rationale": getattr(s, "rationale", None),
            "args": dict(getattr(s, "args", {}) or {}),
            "expected": getattr(s, "expected", None),
            "objective": getattr(s, "objective", None),
            "success_criteria": list(getattr(s, "success_criteria", []) or []),
        }
        for s in plan.steps
    ]


def plan_from_dict(raw: list[dict], mission_id: str) -> Plan:
    steps = [
        PlanStep(
            id=s["id"],
            description=s.get("description", ""),
            action=s.get("action", "analyze"),
            risk=RiskLevel(s.get("risk", RiskLevel.LOW.value)),
            agent=s.get("agent", "general"),
            depends_on=list(s.get("depends_on", [])),
            requires_approval=bool(s.get("requires_approval", False)),
            capability=s.get("capability"),
            requires_input=dict(s.get("requires_input") or {}),
            verification=s.get("verification"),
            proposed_by=s.get("proposed_by"),
            rationale=s.get("rationale"),
            args=dict(s.get("args") or {}),
            expected=s.get("expected"),
            objective=s.get("objective"),
            success_criteria=list(s.get("success_criteria") or []),
        )
        for s in raw
    ]
    return Plan(mission_id, steps)


# --------------------------------------------------------------------------- #
# Req 7 — objetivos y criterios POR PASO, derivados de forma determinista.
#
# Esos campos describen qué le toca a cada paso, pero NADIE los lee para autorizar:
# la autoridad sigue siendo PolicyEngine/AutonomyGate/PlanValidator/GoalVerifier.
# Por eso un modelo o una skill no pueden expandir ni afirmar nada con ellos, y el
# paso no puede usarlos para auto-verificarse.
# --------------------------------------------------------------------------- #


def _step_target(step: PlanStep) -> str | None:
    """El objeto concreto del paso (ruta, recurso, consulta), si sus args lo declaran."""
    args = dict(getattr(step, "args", {}) or {})
    for key in ("path", "target", "file_path", "resource", "source"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def step_objective_for(mission, step: PlanStep) -> str | None:
    """Req 7 — objetivo CONCRETO del paso, derivado determinista y en español.

    Un paso declara la responsabilidad que TIENE, no copia el objetivo completo de la
    misión en cada paso (repetirlo no es planear por pasos). Para `research`/`execute`/
    `write`/etc. se usa el objetivo de la misión como contexto del qué, y la capability/
    args como el cómo.
    """
    action = str(getattr(step, "action", "") or "")
    capability = str(getattr(step, "capability", "") or "") or ACTION_TO_CAPABILITY.get(action, "")
    goal_objective = str(getattr(getattr(mission, "goal", None), "objective", "") or "")
    target = _step_target(step)
    by_action = {
        "understand": (
            f"entender el objetivo «{goal_objective}» y orientar el resto del plan"
        ),
        "analyze": (
            f"analizar el objetivo «{goal_objective}» para decidir los siguientes pasos"
        ),
        "plan": (
            f"organizar la estrategia que resuelve «{goal_objective}»"
        ),
        "recall": "recuperar conocimiento, lecciones y experiencia previa relevante",
        "research": (
            "reunir evidencia del dominio del objetivo"
            + (f" sobre «{target}»" if target else "")
        ),
        "inspect": (
            f"inspeccionar el estado actual de {target or 'lo que el objetivo toca'}"
        ),
        "synthesize": "sintetizar conclusiones a partir de la evidencia reunida",
        "modify": (
            f"aplicar {capability or 'la capability'} para cambiar "
            f"{target or 'el objetivo'}"
        ),
        "write": f"escribir {target or 'el contenido del objetivo'}",
        "remove": f"eliminar {target or 'el objetivo'} de forma controlada",
        "commit": (
            f"aplicar {capability or 'el cambio'} de forma concluyente"
        ),
        "execute": (
            f"ejecutar {capability or 'la capability'} para producir "
            f"{target or 'el efecto declarado del paso'}"
        ),
        "test": f"probar {target or 'el resultado'} para detectar fallos",
        "verify": (
            "verificar de forma independiente que el objetivo de la misión se cumplió"
        ),
        "respond": "responder al usuario con el resultado de la misión",
        "replan": (
            "proponer una estrategia distinta que no repita el fallo registrado"
        ),
    }
    text = by_action.get(action)
    if text is not None:
        return text
    description = str(getattr(step, "description", "") or "")
    return description or (
        f"ejecutar el paso {action} con {capability or 'la capability asignada'}"
    )


#: capabilities de SOLO LECTURA: su criterio es producir una observación, no dejar un
#: estado nuevo. Distinguirlo evita fabricar criterios de mutación donde no los hay.
_READ_ONLY_CAPABILITIES = {
    "fs.read",
    "fs.stat",
    "fs.list",
    "research.filesystem",
    "research.web",
    "verification.filesystem",
    "perception.desktop",
    "web.read",
}


#: Acciones cuyo efecto SOBRE EL MUNDO es comprobable con el vocabulario del
#: `GoalVerifier`. Para ellas el contrato del paso se emite en forma de predicado, y
#: `_absorb` lo evalúa contra la observación real antes de dar el paso por cumplido.
#: P0 §11 / Req #7: sin esto, `tool.success` decidía el paso y el contrato era decorativo.
_EXECUTABLE_STEP_ACTIONS = frozenset({"research", "execute"})

#: Predicado que corresponde a una acción sobre una ruta. El criterio del PASO se deriva
#: de lo que la acción PROMETE hacer, no de lo que el objetivo pide: un paso de escritura
#: promete que el fichero exists; uno de lectura, que su contenido fue observado.
_STEP_PREDICATE_BY_CAPABILITY = {
    "fs.write": "file_exists",
    "fs.remove": "file_missing",
    "fs.read": "content_observed",
    "research.filesystem": "content_observed",
}


def _step_target_path(step: PlanStep) -> str:
    """Ruta que el paso opera, tal y como la ejecutaría la herramienta.

    Se leen los MISMOS `args` que consume el executor, y sólo si son un único segmento:
    si el paso no tiene una ruta inequívoca no se inventa un predicado, porque un
    predicado sobre una ruta inventada comprobaría el fichero equivocado.
    """
    args = getattr(step, "args", None) or {}
    path = args.get("path") if isinstance(args, dict) else None
    if isinstance(path, str) and path and not path.startswith("/") and ".." not in path:
        return path.strip()
    return ""


def step_criteria_for(mission, step: PlanStep) -> list[str]:
    """Req 7 — criterios de éxito DEL PASO, no del objetivo.

    - Son del paso: describen qué evidencia/estado le correspondería a ESTE paso, nunca
      el éxito de la misión entera.
    - Nunca son "el tool devolvió success": el hecho de ejecutar no es un criterio.
    - No otorgan verificación: quién comprueba sigue siendo la observación y la
      verificación independiente (GoalVerifier sigue siendo la autoridad del objetivo).
    - Pasos puramente cognitivos no tienen producto observable propio: devuelven `[]`,
      declarando la limitación en vez de inventar un criterio falso.
    - P0 §11: si el paso tiene un efecto OBSERVABLE sobre una ruta, el criterio se emite
      como PREDICADO comprobable, no como prosa. Ése es el cambio que hace el contrato
      ejecutable: un paso de escritura no se da por cumplido porque la tool dijera que
      tuvo éxito, sino porque se observó que el fichero quedó como el paso prometía.
    """
    action = str(getattr(step, "action", "") or "")
    target = _step_target(step)

    # P0 §11: contrato EJECUTABLE para los pasos que dejan estado observable.
    if action in _EXECUTABLE_STEP_ACTIONS:
        capability = str(getattr(step, "capability", "") or "")
        path = _step_target_path(step)
        predicate = _STEP_PREDICATE_BY_CAPABILITY.get(capability)
        if predicate and path:
            return [f"{predicate}:{path}"]

    if action in ("understand", "analyze", "plan", "recall", "synthesize", "replan"):
        return []
    if action == "research":
        return [
            f"producir una observación registrada sobre "
            f"{target or 'el dominio del objetivo'}"
        ]
    if action == "inspect":
        return [
            f"obtener una observación del estado de {target or 'el objetivo'} "
            "sin modificarlo"
        ]
    if action == "verify":
        return [
            "verificar con observaciones independientes y registrar el resultado"
        ]
    if action == "test":
        return [
            f"dejar registrado el resultado de la prueba sobre "
            f"{target or 'el resultado'}"
        ]
    if action == "respond":
        return [
            "construir la respuesta sobre el resultado verificado de la misión"
        ]
    capability = str(getattr(step, "capability", "") or "")
    if capability in _READ_ONLY_CAPABILITIES:
        return [
            f"obtener una observación registrada de {target or 'lo consultado'} "
            "sin modificar nada"
        ]
    if target:
        return [
            f"{capability or 'la acción'} deja {target} en el estado previsto por el paso"
        ]
    return [
        "el efecto de la acción queda registrado como observación para la verificación"
    ]


def fill_step_contract(mission, step: PlanStep) -> None:
    """Req 7 — completa los campos por-paso cuando faltan. Determinista e idempotente:
    nunca sobreescribe un objetivo/criterio ya escrito (para preservar la traza)."""
    if not step.objective:
        step.objective = step_objective_for(mission, step)
    if not step.success_criteria:
        step.success_criteria = step_criteria_for(mission, step)


def plan_with_contract(mission, steps: list[PlanStep]) -> Plan:
    """Construye un Plan aplicando el contrato Req 7 a cada paso."""
    for step in steps:
        fill_step_contract(mission, step)
    return Plan(mission.id, steps)
