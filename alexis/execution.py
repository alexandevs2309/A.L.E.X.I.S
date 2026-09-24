from alexis.contracts import ExecutionResult, Observation
from alexis.models import ModelRequest, ModelRouter, ModelTask, build_router_from_config
from alexis.models.config import ModelConfig
from alexis.perception.activation import activation_reply, is_activation_objective
from alexis.security.sandbox import SandboxRunner
from alexis.speech.tts import TTSResult, get_tts_provider, synthesize_with_fallback
from alexis.tools.desktop import (
    chrome_open,
    cursor_open,
    desktop_reply,
    desktop_tool_for,
    open_binance,
    open_claude,
    spotify_play,
)
from alexis.tools.filesystem import classify_objective_intent, extract_workspace_path
from alexis.tools.registry import ToolRegistry

ANALYSIS_ACTIONS = {"understand", "analyze", "review"}
ACTION_TOOL = {"research": "fs.read", "execute": "fs.read", "verify": "fs.stat"}

_DESKTOP_HOSTS = {
    "chrome.open_url": lambda a: chrome_open(url=a.get("url") or ""),
    "spotify.play": lambda a: spotify_play(a.get("uri") or ""),
    "claude.open": lambda a: open_claude(),
    "binance.open": lambda a: open_binance(),
    "cursor.open": lambda a: cursor_open(new_window=True),
}


def write_placeholder_content(objective: str) -> str:
    """Contenido determinista que se escribe cuando el objetivo pide crear un archivo
    pero no especifica contenido. Honesto: NO inventa contenido del usuario."""
    return (
        "ALEXIS — archivo creado por misión.\n\n"
        f"Objetivo: {objective}\n"
        "Si querías otro contenido, pedímelo así: «escribe en <archivo>: <contenido>»."
    )


class LocalExecutor:
    """Executor legacy (placeholder explícito). Solo para compatibilidad/tests."""

    async def execute(self, mission, step) -> ExecutionResult:
        return ExecutionResult(
            success=True,
            output={"action": step.action, "description": step.description},
            observations=[
                Observation(
                    source="local_executor",
                    content={"step": step.id, "status": "simulated"},
                    trusted=True,
                )
            ],
        )


class SandboxExecutor:
    """Ejecuta pasos reales: análisis honesto o tool real bajo sandbox.

    - understand/analyze → resultado de análisis (sin tool; no es falso).
    - research/execute/verify → tool del registry mapeada por acción, bajo subprocess
      restringido; si la tool no existe, el paso FALLA con error honesto.
    """

    def __init__(
        self,
        tools: ToolRegistry,
        sandbox: SandboxRunner,
        action_map: dict[str, str] | None = None,
        default_path: str = "README.txt",
        desktop_delegate: str | None = None,
        model_router: ModelRouter | None = None,
    ):
        self.tools = tools
        self.sandbox = sandbox
        self.action_map = action_map or dict(ACTION_TOOL)
        self.default_path = default_path
        self.desktop_delegate = desktop_delegate
        #: Router de modelos (Gemini/Ollama) para generar la respuesta hablada.
        #: ``None`` = se construye desde el entorno; sin provider REAL se cae a frases fijas.
        self.model_router = model_router

    async def _spoken_reply(self, mission, desktop) -> str:
        """Respuesta hablada generada por el Model Router si hay un provider REAL.

        Devuelve ``""`` cuando no hay provider (o sólo contingencia) para que el
        caller caiga a las frases deterministas: la contingencia (eco) NUNCA se
        presenta como si pensara. El saludo de activación (palmada) también es
        fijo a propósito, para que ALEXIS salude igual siempre.
        """
        objective = mission.goal.objective
        if desktop is None and is_activation_objective(objective):
            return ""
        router = self.model_router
        if router is None:
            router = build_router_from_config(ModelConfig.from_env())
        if not router.providers():
            return ""
        prompt = f"Pedido del usuario: {objective!r}."
        if desktop is not None:
            tool_name, args = desktop
            prompt += (
                f"\nSe ejecutó la herramienta de escritorio {tool_name}"
                + (f" con argumentos {dict(args)}" if args else "")
                + "."
            )
        system = (
            "Eres ALEXIS, un asistente de voz en español. "
            "Tu respuesta se convertirá en voz: escribe UNA sola frase breve, natural "
            "y conversacional (como la dirías en voz alta), sin markdown, sin listas, "
            "sin emojis y sin inventar acciones que no se ejecutaron."
        )
        try:
            response = await router.complete(
                ModelRequest(
                    task=ModelTask.SYNTHESIZE,
                    system=system,
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=80,
                    temperature=0.7,
                    deadline_ms=15000,
                )
            )
        except Exception:  # noqa: BLE001 — la voz nunca debe fallar por el modelo
            return ""
        if not response.is_real or response.error or not (response.text or "").strip():
            return ""
        spoken = " ".join(response.text.split())[:300].strip().strip('"“”')
        return spoken

    async def _run_tool(self, mission, step, tool_name: str, extra: dict | None = None) -> ExecutionResult:
        try:
            tool = self.tools.get(tool_name)
        except KeyError:
            return ExecutionResult(
                success=False,
                error=f"no hay tool registrada '{tool_name}'",
                observations=[Observation(f"tool.{tool_name}", {"status": "missing"}, trusted=True)],
            )
        path = extract_workspace_path(mission.goal.objective) or self.default_path
        args = {"path": path}
        if extra:
            args.update(extra)
        try:
            output = await tool.handler(args)
        except Exception as exc:  # noqa: BLE001 — el error debe terminar el paso, no la misión
            output = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        ok = isinstance(output, dict) and output.get("ok") is True
        observation = Observation(f"tool.{tool_name}", output, trusted=True)
        if not ok:
            return ExecutionResult(
                success=False,
                output=output,
                error=output.get("error") if isinstance(output, dict) else str(output),
                observations=[observation],
            )
        return ExecutionResult(success=True, output=output, observations=[observation])

    async def _dispatch_desktop(self, mission, step, desktop: tuple[str, dict]) -> ExecutionResult:
        tool_name, extra = desktop
        if self.tools.has(tool_name):
            tool = self.tools.get(tool_name)
            try:
                output = await tool.handler(**extra)
            except Exception as exc:  # noqa: BLE001 — el error termina el paso, no la misión
                output = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        elif self.desktop_delegate == "host":
            output = await self._host_desktop(tool_name, extra)
        else:
            return ExecutionResult(
                success=False,
                error=f"no hay tool registrada '{tool_name}'",
                observations=[Observation(f"tool.{tool_name}", {"status": "missing"}, trusted=True)],
            )
        ok = isinstance(output, dict) and output.get("ok") is True
        observation = Observation(f"tool.{tool_name}", output, trusted=True)
        if not ok:
            return ExecutionResult(
                success=False,
                output=output,
                error=output.get("error") if isinstance(output, dict) else str(output),
                observations=[observation],
            )
        return ExecutionResult(success=True, output=output, observations=[observation])

    async def _host_desktop(self, tool_name: str, args: dict) -> dict:
        if tool_name == "tts.speak":
            text = (args.get("text") or "").strip()
            if not text:
                return {"ok": False, "reason": "sin texto"}
            tts: TTSResult = await get_tts_provider().synthesize(text)
            return {
                "ok": tts.ok,
                "text": text,
                "provider": tts.provider,
                "message": tts.message,
                "path": tts.path,
                "format": tts.format,
                "error": tts.error,
            }
        handler = _DESKTOP_HOSTS.get(tool_name)
        if handler is None:
            return {"ok": False, "error": f"tool de escritorio '{tool_name}' no soportada por el delegado host"}
        try:
            return handler(args)
        except Exception as exc:  # noqa: BLE001 — razones honestas, sin fingir éxito
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    async def execute(self, mission, step) -> ExecutionResult:
        action = step.action
        if action == "respond":
            desktop = desktop_tool_for(mission.goal.objective)
            message = await self._spoken_reply(mission, desktop)
            if not message:
                if desktop is not None:
                    message = desktop_reply(desktop[0], **desktop[1])
                else:
                    message = activation_reply(mission.goal.objective)
            tts: TTSResult = await synthesize_with_fallback(message, provider=get_tts_provider())
            return ExecutionResult(
                success=True,
                output={
                    "message": message,
                    "action": "respond",
                    "objective": mission.goal.objective,
                    "tts": {
                        "ok": tts.ok,
                        "provider": tts.provider,
                        "message": tts.message,
                        "path": tts.path,
                        "error": tts.error,
                    },
                },
                observations=[
                    Observation(
                        source="agent",
                        content={
                            "step": step.id,
                            "response": message,
                            "tts_provider": tts.provider,
                            "tts_ok": tts.ok,
                            "tts_path": tts.path,
                        },
                        trusted=True,
                    )
                ],
            )

        desktop = desktop_tool_for(mission.goal.objective)
        if action == "execute" and desktop is not None:
            return await self._dispatch_desktop(mission, step, desktop)

        intent = classify_objective_intent(mission.goal.objective)
        if action in ANALYSIS_ACTIONS:
            path = extract_workspace_path(mission.goal.objective) or self.default_path
            return ExecutionResult(
                success=True,
                output={
                    "analysis": step.description,
                    "objective": mission.goal.objective,
                    "target_path": path,
                    "constraints": mission.goal.constraints,
                    "intent": intent,
                },
                observations=[
                    Observation(
                        source="planner",
                        content={"step": step.id, "analysis": step.description, "target_path": path, "intent": intent},
                        trusted=True,
                    )
                ],
            )

        if action == "research":
            tool_name = "fs.stat" if intent in {"write", "destructive"} else "fs.read"
        elif action == "execute":
            tool_name = "fs.remove" if intent == "destructive" else ("fs.write" if intent == "write" else "fs.read")
        else:
            tool_name = self.action_map.get(action)

        if intent == "unsupported" and action == "execute":
            return ExecutionResult(
                success=False,
                error=(
                    "Renombrar, mover o copiar no está soportado en M1. "
                    "Puedo crear/editar archivos (sin permiso) o borrarlos (con tu aprobación)."
                ),
                observations=[
                    Observation("executor", {"status": "unsupported", "objective": mission.goal.objective}, trusted=True)
                ],
            )

        if tool_name is None:
            return ExecutionResult(
                success=False,
                error=f"acción '{action}' no mapeada a ninguna tool",
                observations=[Observation("executor", {"status": "no_tool", "action": action}, trusted=True)],
            )

        if tool_name == "fs.write":
            path = extract_workspace_path(mission.goal.objective) or self.default_path
            return await self._run_tool(
                mission,
                step,
                tool_name,
                {"path": path, "content": write_placeholder_content(mission.goal.objective), "overwrite": True},
            )

        if tool_name == "fs.remove":
            path = extract_workspace_path(mission.goal.objective)
            if not path:
                return ExecutionResult(
                    success=False,
                    error="No nombraste el archivo a borrar (ej.: «borra el archivo notas.txt»).",
                    observations=[Observation("executor", {"status": "no_path", "action": action}, trusted=True)],
                )
            return await self._run_tool(mission, step, tool_name)

        return await self._run_tool(mission, step, tool_name)