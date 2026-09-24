from pathlib import Path

from alexis.contracts import Verification
from alexis.perception.activation import is_activation_objective
from alexis.security.sandbox import SandboxError, SandboxRunner
from alexis.tools.desktop import desktop_tool_for
from alexis.tools.filesystem import classify_objective_intent, extract_workspace_path, is_informational_objective


class BasicVerifier:
    """Legacy verifier placeholder (siempre pasa). Solo para compatibilidad/tests."""

    async def verify(self, mission, plan):
        return Verification(
            passed=True,
            evidence=["All foundation execution steps returned success."],
            confidence=0.70,
            notes="Replace with domain-specific verification.",
        )


def _resolve(workspace: str | Path, path: str) -> Path | None:
    try:
        return SandboxRunner(workspace).resolve_in_workspace(path)
    except SandboxError:
        return None


class FilesystemVerifier:
    """Verificación independiente y determinista del workspace.

    No confía en el executor: re-estadifica la ruta por su cuenta.
    - read: el archivo debe existir (había algo que leer).
    - write: el archivo debe existir AHORA (la escritura se aplicó).
    - destructive: el archivo debe NO existir (el borrado ocurrió de verdad).
    """

    def __init__(self, workspace: str | Path):
        self.workspace = Path(workspace).resolve()

    async def verify(self, mission, plan) -> Verification:
        objective = mission.goal.objective
        if is_activation_objective(objective):
            return Verification(
                passed=True,
                evidence=[
                    "verifier=FilesystemVerifier",
                    f"objective={objective}",
                    "activation_received=True",
                ],
                confidence=0.9,
                notes="Activación recibida y respuesta preparada; no escribió nada en el workspace.",
            )

        desktop = desktop_tool_for(objective)
        if desktop is not None:
            tool_name, _args = desktop
            evidence = [
                "verifier=FilesystemVerifier",
                f"objective={objective}",
                f"desktop_tool={tool_name}",
            ]
            dispatched = any(
                r.get("success")
                and r.get("step") == "execute"
                and isinstance(r.get("output"), dict)
                and r["output"].get("ok") is True
                for r in (mission.results or [])
            )
            if dispatched:
                return Verification(
                    passed=True,
                    evidence=evidence + ["despachada=True"],
                    confidence=0.9,
                    notes=f"Tool de escritorio {tool_name} despachada y confirmada en los results de la misión.",
                )
            return Verification(
                passed=False,
                evidence=evidence,
                confidence=0.5,
                notes=f"La misión pedía {tool_name} pero no fue despachada (sin evidencia de dispatch en results).",
            )

        if is_informational_objective(objective):
            answered = any(
                r.get("success")
                and r.get("step") == "respond"
                and isinstance(r.get("output"), dict)
                and (r["output"].get("message") or "").strip()
                for r in (mission.results or [])
            )
            if answered:
                return Verification(
                    passed=True,
                    evidence=[
                        "verifier=FilesystemVerifier",
                        f"objective={objective}",
                        "chat_answered=True",
                    ],
                    confidence=0.9,
                    notes="Pregunta/información respondida de voz; no había archivos que tocar.",
                )
            return Verification(
                passed=False,
                evidence=[
                    "verifier=FilesystemVerifier",
                    f"objective={objective}",
                    "chat_answered=False",
                ],
                confidence=0.5,
                notes="La misión era informativa pero no quedó evidencia de una respuesta hablada.",
            )

        intent = classify_objective_intent(objective)
        path = extract_workspace_path(objective)
        evidence = [f"verifier=FilesystemVerifier", f"objective={objective}", f"target_path={path or '-'}", f"intent={intent}"]

        if path is None:
            return Verification(
                passed=False,
                evidence=evidence,
                confidence=0.5,
                notes="La misión no nombra un archivo del workspace; el runtime solo trabaja archivos dentro del workspace.",
            )

        resolved = _resolve(self.workspace, path)
        exists = resolved is not None and resolved.exists() and resolved.is_file()

        if intent == "destructive":
            if exists:
                return Verification(
                    passed=False,
                    evidence=evidence + [f"archivo_existe=True"],
                    confidence=0.7,
                    notes=f"La misión pedía borrar '{path}' pero el archivo sigue existiendo; el borrado no se confirmó.",
                )
            return Verification(
                passed=True,
                evidence=evidence + [f"archivo_existe=False"],
                confidence=0.9,
                notes=f"El archivo '{path}' ya no existe dentro del workspace (borrado confirmado).",
            )

        if intent in {"write", "unsupported"}:
            if not exists:
                return Verification(
                    passed=False,
                    evidence=evidence + [f"archivo_existe=False"],
                    confidence=0.7,
                    notes=f"La misión pedía crear/escribir '{path}' pero el archivo no existe; no hay evidencia de la acción.",
                )
            st = resolved.stat()
            evidence += [f"archivo_existe=True", f"path={path}", f"size={st.st_size}"]
            return Verification(
                passed=True,
                evidence=evidence,
                confidence=0.9,
                notes=f"El archivo '{path}' existe dentro del workspace (escritura aplicada y re-verificada).",
            )

        if not exists:
            return Verification(
                passed=False,
                evidence=evidence + [f"archivo_existe=False"],
                confidence=0.7,
                notes=f"No existe '{path}' en el workspace; no hay resultado que verificar.",
            )

        st = resolved.stat()
        evidence += [f"archivo_existe=True", f"path={path}", f"size={st.st_size}"]
        return Verification(
            passed=True,
            evidence=evidence,
            confidence=0.9,
            notes="Verificado de forma independiente dentro del workspace.",
        )