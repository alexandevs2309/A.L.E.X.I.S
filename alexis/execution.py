import asyncio
import time
import typing

from alexis.contracts import ExecutionResult, Observation
from alexis.models import ModelRequest, ModelRouter, ModelTask
from alexis.models.config import ModelConfig
from alexis.perception.activation import activation_reply, is_activation_objective
from alexis.security.sandbox import SandboxError, SandboxRunner
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
from alexis.tools.filesystem import classify_objective_intent, extract_workspace_path, is_informational_objective
from alexis.tools.registry import ToolRegistry

ANALYSIS_ACTIONS = {"understand", "analyze", "review"}
ACTION_TOOL = {"research": "fs.read", "execute": "fs.read", "verify": "fs.stat"}

#: Capabilities que identifican su tool sin ambigüedad. Si el paso la declara, manda la
#: capability y no la acción: validar `fs.stat` y ejecutar `fs.read` sería incoherente.
_CAPABILITY_TOOL = {
    "fs.read": "fs.read",
    "fs.stat": "fs.stat",
    "fs.write": "fs.write",
    "fs.remove": "fs.remove",
    # P0 §5.4: sin esta entrada, un paso `action=test` con `capability=execute.test`
    # llegaba al executor sin tool y moría con "acción 'test' no mapeada a ninguna tool".
    "execute.test": "execute.test",
}

#: Capabilities cuyo `path` vive dentro del sandbox del proyecto.
_SANDBOX_CAPABILITY_PREFIXES = ("fs.", "research.", "verification.")

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
        voice_mode_provider: typing.Callable[[], bool] | None = None,
    ):
        self.tools = tools
        self.sandbox = sandbox
        self.action_map = action_map or dict(ACTION_TOOL)
        self.default_path = default_path
        self.desktop_delegate = desktop_delegate
        #: Router de modelos (Gemini/Ollama) para generar la respuesta hablada.
        #: ``None`` = se construye desde el entorno; sin provider REAL se cae a frases fijas.
        self.model_router = model_router
        #: Modo voz del demo (como el toggle de ChatGPT). ``None`` = siempre voz.
        #: Si devuelve False, el `respond` NO sintetiza audio (modo solo texto).
        self.voice_mode_provider = voice_mode_provider

    async def _spoken_reply(self, mission, desktop) -> str:
        """Respuesta hablada generada por un modelo REAL (Gemini/Ollama).

        En producción (sin ``model_router`` inyectado) se lanzan los providers en
        paralelo y gana el primero que devuelva texto válido: ALEXIS responde con la
        latencia del proveedor más rápido en cada momento. Devuelve ``""`` cuando
        ninguno responde para que el caller caiga a las frases honestas: la
        contingencia (eco) NUNCA se presenta como si pensara. El saludo de activación
        (palmada) es fijo a propósito.
        """
        objective = mission.goal.objective
        if desktop is None and is_activation_objective(objective):
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
            "Eres ALEXIS, un asistente de voz en español. Responde lo que se te pida "
            "con la información útil y concreta en UNA o DOS frases breves (máximo 45 "
            "palabras), natural y conversacional, sin markdown, sin listas, sin emojis, "
            "terminando con punto, y sin inventar acciones que no se ejecutaron."
        )
        request = ModelRequest(
            task=ModelTask.SYNTHESIZE,
            system=system,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=70,
            temperature=0.7,
            deadline_ms=20000,
        )
        if self.model_router is not None:
            if not self.model_router.providers():
                return ""
            try:
                response = await self.model_router.complete(request)
            except Exception:  # noqa: BLE001 — la voz nunca debe fallar por el modelo
                return ""
            return self._valid_text(response)
        best = ""
        for _ in range(2):
            try:
                text = await self._race_providers(ModelConfig.from_env().build_providers(), request)
            except Exception:  # noqa: BLE001 — red/tiempos nunca tumban la voz
                break
            if not text:
                break
            best = text
            if self._complete_enough(text):
                return text
        return self._repair_punctuation(best)

    @staticmethod
    def _valid_text(response) -> str:
        if not response or not response.is_real or response.error or not (response.text or "").strip():
            return ""
        return " ".join(response.text.split())[:500].strip().strip('"“”')

    @staticmethod
    def _complete_enough(text: str) -> bool:
        return len(text) >= 30 and text.rstrip().endswith((".", "!", "?", "…", "”", '"', ")"))

    @staticmethod
    def _repair_punctuation(text: str) -> str:
        """Cierra la respuesta si el modelo se quedó a medio camino (sin puntuación)."""
        t = " ".join(text.split()).strip()
        if not t:
            return ""
        last = t[-1]
        if last in ".!?:…\"”¿¡'":
            return t
        idx = max(t.rfind("."), t.rfind("!"), t.rfind("?"))
        if idx > len(t) * 0.4:
            t = t[: idx + 1].strip()
        if t and t[-1] not in ".!?":
            t += "."
        return t

    async def _race_providers(self, providers: list, request: ModelRequest) -> str:
        """Lanza todos los providers a la vez; devuelve el primer texto REAL válido."""
        usable = [p for p in providers if getattr(p, "available", False)]
        if not usable:
            return ""
        per_provider_timeout = max(request.deadline_ms, 1000) / 1000.0
        tasks = {
            asyncio.create_task(
                asyncio.wait_for(p.complete(request), timeout=per_provider_timeout)
            )
            for p in usable
        }
        wall = time.monotonic() + per_provider_timeout + 1.0
        pending = set(tasks)
        try:
            while pending:
                left = wall - time.monotonic()
                if left <= 0:
                    break
                done, pending = await asyncio.wait(
                    pending, timeout=left, return_when=asyncio.FIRST_COMPLETED
                )
                for task in done:
                    try:
                        text = self._valid_text(task.result())
                    except Exception:  # noqa: BLE001
                        continue
                    if text:
                        return text
        finally:
            for task in pending:
                task.cancel()
        return ""

    async def _run_tool(self, mission, step, tool_name: str, args: dict | None = None) -> ExecutionResult:
        """Ejecuta una tool con arguments ya decididos.

        `args` es lo que recibe la herramienta, sin fusiones posteriores. Si no se pasa,
        la fuente es `PlanStep.args` (H2) y, en su defecto, el path legacy derivado del
        objetivo. Nunca se mezclan ambos: no hay un segundo origen de verdad.
        """
        try:
            tool = self.tools.get(tool_name)
        except KeyError:
            return ExecutionResult(
                success=False,
                error=f"no hay tool registrada '{tool_name}'",
                observations=[Observation(f"tool.{tool_name}", {"status": "missing"}, trusted=True)],
            )
        final_args = dict(args) if args is not None else self._step_args(mission, step)
        denial = self._perimeter_denial(step, tool_name, final_args)
        if denial:
            return ExecutionResult(
                success=False,
                error=denial,
                observations=[Observation(f"tool.{tool_name}", {"status": "out_of_perimeter"}, trusted=True)],
            )
        try:
            output = await tool.handler(final_args)
        except Exception as exc:  # noqa: BLE001 — el error debe terminar el paso, no la misión
            output = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        ok = isinstance(output, dict) and output.get("ok") is True
        observation = Observation(f"tool.{tool_name}", {"args": final_args, **output} if isinstance(output, dict) else output, trusted=True)
        if not ok:
            return ExecutionResult(
                success=False,
                output=output,
                error=output.get("error") if isinstance(output, dict) else str(output),
                observations=[observation],
            )
        return ExecutionResult(success=True, output=output, observations=[observation])

    def _step_args(self, mission, step, *, allow_default: bool = True) -> dict:
        """Argumentos del paso. `PlanStep.args` es la fuente de verdad (H2).

        Sin args en el paso (planner por reglas, camino legacy) se mantiene la
        derivación histórica desde el objetivo. `allow_default=False` para operaciones
        destructivas: si el objetivo no nombra un archivo, se falla con honestidad en
        lugar de apuntar a un path por defecto.
        """
        validated = dict(getattr(step, "args", {}) or {})
        if validated:
            return validated
        path = extract_workspace_path(mission.goal.objective)
        if path:
            return {"path": path}
        return {"path": self.default_path} if allow_default else {}

    def _perimeter_denial(self, step, tool_name: str, args: dict) -> str | None:
        """Última frontera: el path que se va a ejecutar debe estar en el perímetro.

        La validación del plan ya avisa de esto con un motivo legible; esto cubre el
        caso en que los args cambian DESPUÉS de validarse: la herramienta ni se llama.
        """
        capability = getattr(step, "capability", None) or ""
        if not capability.startswith(_SANDBOX_CAPABILITY_PREFIXES):
            return None
        path = args.get("path")
        if not isinstance(path, str) or not path:
            return None
        try:
            self.sandbox.resolve_in_workspace(path)
        except SandboxError as exc:
            return (
                f"el paso '{getattr(step, 'id', '?')}' pide '{path}', que está fuera del "
                f"perímetro autorizado: {exc}"
            )
        return None

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

    async def execute(self, mission, step, *, tool_name: str | None = None) -> ExecutionResult:
        """Ejecuta un paso.

        `tool_name` es el override que el Cognitive Runtime puede proponer al replanear
        (misma acción, tool distinta). Por defecto es `None`: el comportamiento legacy
        (acción → tool por `ACTION_TOOL` + intención del objetivo) no cambia.
        """
        action = step.action
        if action == "respond":
            desktop = desktop_tool_for(mission.goal.objective)
            message = await self._spoken_reply(mission, desktop)
            if not message:
                if desktop is not None:
                    message = desktop_reply(desktop[0], **desktop[1])
                elif is_informational_objective(mission.goal.objective):
                    message = (
                        "No pude responder eso ahora mismo con mi modelo; "
                        "vuelve a intentarlo en unos segundos."
                    )
                else:
                    message = activation_reply(mission.goal.objective)
            voice_on = self.voice_mode_provider() if self.voice_mode_provider is not None else True
            if voice_on:
                tts: TTSResult = await synthesize_with_fallback(message, provider=get_tts_provider())
            else:
                tts = TTSResult(
                    ok=False,
                    provider="voice-mode-off",
                    message="modo voz desactivado (respuesta solo en texto)",
                    path=None,
                    format="none",
                    error="voice_mode_off",
                )
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
            return ExecutionResult(
                success=True,
                output={
                    "analysis": step.description,
                    "objective": mission.goal.objective,
                    "target_path": self._step_args(mission, step).get("path"),
                    "constraints": mission.goal.constraints,
                    "intent": intent,
                },
                observations=[
                    Observation(
                        source="planner",
                        content={
                            "step": step.id,
                            "analysis": step.description,
                            "target_path": self._step_args(mission, step).get("path"),
                            "intent": intent,
                        },
                        trusted=True,
                    )
                ],
            )

        override = tool_name
        if override is None:
            override = _CAPABILITY_TOOL.get(getattr(step, "capability", None) or "")
        if action == "research":
            resolved_tool = "fs.stat" if intent in {"write", "destructive"} else "fs.read"
        elif action == "execute":
            resolved_tool = "fs.remove" if intent == "destructive" else ("fs.write" if intent == "write" else "fs.read")
        else:
            resolved_tool = self.action_map.get(action)
        tool_name = override or resolved_tool

        if intent == "unsupported" and action == "execute" and override is None:
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
            args = self._step_args(mission, step)
            args.setdefault("content", write_placeholder_content(mission.goal.objective))
            args.setdefault("overwrite", True)
            return await self._run_tool(mission, step, tool_name, args)

        if tool_name == "fs.remove":
            args = self._step_args(mission, step, allow_default=False)
            if not args.get("path"):
                return ExecutionResult(
                    success=False,
                    error="No nombraste el archivo a borrar (ej.: «borra el archivo notas.txt»).",
                    observations=[Observation("executor", {"status": "no_path", "action": action}, trusted=True)],
                )
            return await self._run_tool(mission, step, tool_name, args)

        return await self._run_tool(mission, step, tool_name)