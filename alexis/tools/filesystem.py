import json
import re
import sys
import time
from pathlib import Path

from alexis.security.sandbox import SandboxError, SandboxRunner
from alexis.tools.registry import Tool

PATH_PATTERN = re.compile(r"([\w./\-]+\.(?:txt|md|json|log|csv|py|ini|env|yaml|yml))")

WRITE_INTENT_KEYWORDS = (
    "crea", "crear", "creame", "escribe", "escribir", "escribe en", "guarda", "guardar",
    "guardame", "agrega", "agregar", "añade", "añadir", "modifica", "modificar", "edita",
    "editar", "actualiza", "actualizar", "genera", "generar", "creame", "nuevo archivo",
    "nueva archivo", "create", "write",
)

DESTRUCTIVE_KEYWORDS = (
    "borra", "borrar", "borrame", "elimina", "eliminar", "eliminame", "quita", "quitar",
    "sobreescribe", "sobreescribir", "delete", "remove",
)

UNSUPPORTED_KEYWORDS = (
    "renombra", "renombrar", "mueve", "mover", "copia", "copiar",
)


def classify_objective_intent(objective: str) -> str:
    """Clasifica el objetivo en read | write | destructive | unsupported.

    Es un ORÁCULO de intención: un input más para la Policy Engine, NO la fuente
    única de permisos (el permiso lo decide la política + el envelope, ver
    docs/AUTONOMY-V0.5-CAPABILITIES.md). Destructivo (borrar/sobreescribir) solo
    marca la intención delicada; la aprobación la decide la regla de política.
    Determinista por palabras clave; sin IA."""
    low = objective.lower()
    if any(keyword in low for keyword in DESTRUCTIVE_KEYWORDS):
        return "destructive"
    if any(keyword in low for keyword in UNSUPPORTED_KEYWORDS):
        return "unsupported"
    if any(keyword in low for keyword in WRITE_INTENT_KEYWORDS):
        return "write"
    return "read"


def detect_write_intent(objective: str) -> bool:
    """True si la misión pedirá mutar el workspace de forma NO delicada (crear/editar)."""
    return classify_objective_intent(objective) == "write"


def detect_destructive_intent(objective: str) -> bool:
    """True solo para tareas DELICADAS: borrar/eliminar/sobreescribir archivos."""
    return classify_objective_intent(objective) == "destructive"


def extract_workspace_path(objective: str) -> str | None:
    """Ruta relativa en el workspace mencionada en el objetivo (determinista)."""
    m = PATH_PATTERN.search(objective)
    if not m:
        return None
    return m.group(1).strip("./")


#: Marcas de texto que hacen de un objetivo una petición INFORMATIVA (pregunta o
#: conversación) en lugar de una tarea de archivos. Para que la ruta chat entre,
#: además, el objetivo NO puede nombrar un archivo del workspace ni tener un
#: intent de acción (escribir/borrar/soportado).
CONVERSATIONAL_MARKERS = (
    "?",
    "qué es", "que es", "qué ", "que ",
    "cómo", "como ", "cuándo", "cuando ", "dónde", "donde ",
    "quién", "quien ", "cuál", "cual ", "cuánto", "cuanto ",
    "por qué", "por que", "porque ",
    "dime", "dime ", "cuéntame", "cuentame ",
    "háblame", "hablame ", "conversa", "habla",
    "resumen", "resume ", "explica",
    "qué puedes", "que puedes", "qué eres", "que eres", "quién eres", "quien eres",
    "eres", "información", "info sobre",
    "recomiénd", "recomienda", "recomiende", "sugiere", "sugiero", "sugerir",
    "propón", "propon ", "dame ", "dame", "quiero saber", "podrías", "podrias ",
    "puedes ", "consejo", "sugerencia", "ayúdame", "ayudame",
    "el mejor", "la mejor", "cuál crees", "cual crees", "pídele", "pideme",
)


def is_informational_objective(objective: str) -> bool:
    """True si el objetivo es una pregunta/información, no una tarea accionable.

    No es IA: reglas deterministas. La activación (palmada) y las órdenes de
    escritorio se deciden antes, así que aquí solo excluimos archivos reales y
    mutations."""
    low = " ".join((objective or "").lower().split())
    if not low:
        return False
    if extract_workspace_path(low):
        return False
    if classify_objective_intent(low) != "read":
        return False
    return any(m in low for m in CONVERSATIONAL_MARKERS)


def _load_result(stdout: str):
    marker = "ALEXIS_RESULT="
    idx = stdout.rfind(marker)
    if idx == -1:
        return {"ok": False, "error": "salida de tool ilegible", "raw": stdout[:400]}
    try:
        return json.loads(stdout[idx + len(marker):].strip())
    except ValueError:
        return {"ok": False, "error": "JSON de tool inválido", "raw": stdout[:400]}


class FileSystemReadTool:
    name = "fs.read"
    description = "Lee un archivo del workspace autorizado (solo lectura, sin red)."
    risk = "low"

    def __init__(self, workspace: str | Path, max_bytes: int = 64 * 1024):
        self.runner = SandboxRunner(workspace)
        self.workspace = str(Path(workspace).resolve())
        self.max_bytes = max_bytes
        self.timeout = 5.0
        self.permissions = {"workspace": self.workspace, "read": True, "write": False}
        self.limits = {"max_bytes": max_bytes}
        self.schema = {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "ruta relativa dentro del workspace"},
                "max_bytes": {"type": "integer"},
            },
            "required": ["path"],
        }
        self.handler = self._read

    async def _read(self, args: dict) -> dict:
        path = str(args.get("path", ""))
        max_bytes = int(args.get("max_bytes", self.max_bytes))
        if not path or ".." in path:
            return {"ok": False, "error": "path inválido"}
        try:
            self.runner.resolve_in_workspace(path)
        except SandboxError as exc:
            return {"ok": False, "error": str(exc)}

        script = (
            "import json,sys,pathlib\n"
            "root=pathlib.Path(sys.argv[1]).resolve()\n"
            "p=pathlib.Path(sys.argv[2]).resolve()\n"
            "maxb=int(sys.argv[3])\n"
            "try:\n p.relative_to(root)\nexcept ValueError:\n"
            " print('ALEXIS_RESULT='+json.dumps({'ok':False,'error':'fuera de workspace'}));sys.exit(0)\n"
            "if not p.is_file():\n"
            " print('ALEXIS_RESULT='+json.dumps({'ok':False,'error':'no existe: '+str(p)}));sys.exit(0)\n"
            "data=p.read_bytes()[:maxb]\n"
            "text=data.decode('utf-8','replace')\n"
            "res={'ok':True,'path':str(p.relative_to(root)),'size':len(data),'truncated':len(data)>=maxb,'content':text}\n"
            "print('ALEXIS_RESULT='+json.dumps(res,ensure_ascii=False))"
        )
        result = await self.runner.run([sys.executable, "-c", script, self.workspace, str(Path(self.workspace) / path), str(max_bytes)])
        payload = _load_result(result.stdout)
        if result.timed_out or not result.ok:
            payload = {"ok": False, "error": payload.get("error") or "timeout o fallo de sandbox", "raw": result.stdout[:300]}
        payload["elapsed_ms"] = result.elapsed_ms
        return payload


class FileSystemStatTool:
    name = "fs.stat"
    description = "Comprueba existencia, tipo y tamaño de una ruta del workspace (verificación independiente)."
    risk = "low"

    def __init__(self, workspace: str | Path):
        self.runner = SandboxRunner(workspace)
        self.workspace = str(Path(workspace).resolve())
        self.timeout = 5.0
        self.permissions = {"workspace": self.workspace, "read": True, "write": False}
        self.limits = {}
        self.schema = {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        }
        self.handler = self._stat

    async def _stat(self, args: dict) -> dict:
        path = str(args.get("path", ""))
        if not path or ".." in path:
            return {"ok": False, "error": "path inválido"}
        try:
            self.runner.resolve_in_workspace(path)
        except SandboxError as exc:
            return {"ok": False, "error": str(exc)}
        script = (
            "import json,sys,pathlib,time as _t\n"
            "root=pathlib.Path(sys.argv[1]).resolve()\n"
            "p=pathlib.Path(sys.argv[2]).resolve()\n"
            "try:\n p.relative_to(root)\nexcept ValueError:\n"
            " print('ALEXIS_RESULT='+json.dumps({'ok':False,'error':'fuera de workspace'}));sys.exit(0)\n"
            "if not p.exists():\n"
            " print('ALEXIS_RESULT='+json.dumps({'ok':True,'path':str(p.relative_to(root)),'exists':False}));sys.exit(0)\n"
            "st=p.stat()\n"
            "res={'ok':True,'path':str(p.relative_to(root)),'exists':True,'type':'dir' if p.is_dir() else 'file','size':st.st_size}\n"
            "print('ALEXIS_RESULT='+json.dumps(res,ensure_ascii=False))"
        )
        result = await self.runner.run([sys.executable, "-c", script, self.workspace, str(Path(self.workspace) / path)])
        payload = _load_result(result.stdout)
        if result.timed_out or not result.ok:
            payload = {"ok": False, "error": payload.get("error") or "timeout o fallo de sandbox"}
        payload["verified_at"] = time.time()
        return payload


class FileSystemWriteTool:
    name = "fs.write"
    description = "Crea o sobrescribe un archivo dentro del workspace (texto, limitado, sin red)."
    risk = "medium"

    def __init__(self, workspace: str | Path, max_bytes: int = 32 * 1024):
        self.runner = SandboxRunner(workspace)
        self.workspace = str(Path(workspace).resolve())
        self.max_bytes = max_bytes
        self.timeout = 5.0
        self.permissions = {"workspace": self.workspace, "read": True, "write": True}
        self.limits = {"max_bytes": max_bytes}
        self.schema = {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "ruta relativa dentro del workspace"},
                "content": {"type": "string", "description": "contenido de texto a escribir"},
                "overwrite": {"type": "boolean", "description": "permite sobrescribir si ya existe"},
            },
            "required": ["path"],
        }
        self.handler = self._write

    async def _write(self, args: dict) -> dict:
        path = str(args.get("path", ""))
        content = str(args.get("content", ""))
        overwrite = bool(args.get("overwrite", False))
        if not path or ".." in path:
            return {"ok": False, "error": "path inválido"}
        try:
            self.runner.resolve_in_workspace(path)
        except SandboxError as exc:
            return {"ok": False, "error": str(exc)}
        if len(content.encode("utf-8")) > self.max_bytes:
            content = content.encode("utf-8")[: self.max_bytes].decode("utf-8", "ignore")

        script = (
            "import json,sys,pathlib\n"
            "root=pathlib.Path(sys.argv[1]).resolve()\n"
            "p=pathlib.Path(sys.argv[2]).resolve()\n"
            "overwrite=sys.argv[3]=='1'\n"
            "content=sys.argv[4]\n"
            "try:\n p.relative_to(root)\nexcept ValueError:\n"
            " print('ALEXIS_RESULT='+json.dumps({'ok':False,'error':'fuera de workspace'}));sys.exit(0)\n"
            "if p.exists() and not overwrite:\n"
            " print('ALEXIS_RESULT='+json.dumps({'ok':False,'error':'ya existe: '+str(p)}));sys.exit(0)\n"
            "p.parent.mkdir(parents=True,exist_ok=True)\n"
            "p.write_text(content,encoding='utf-8')\n"
            "res={'ok':True,'path':str(p.relative_to(root)),'origin':'overwritten' if p.exists() and overwrite else 'created','size':p.stat().st_size}\n"
            "print('ALEXIS_RESULT='+json.dumps(res,ensure_ascii=False))"
        )
        result = await self.runner.run(
            [sys.executable, "-c", script, self.workspace, str(Path(self.workspace) / path), "1" if overwrite else "0", content]
        )
        payload = _load_result(result.stdout)
        if result.timed_out or not result.ok:
            payload = {"ok": False, "error": payload.get("error") or "timeout o fallo de sandbox", "raw": result.stdout[:300]}
        payload["elapsed_ms"] = result.elapsed_ms
        return payload


class FileSystemRemoveTool:
    name = "fs.remove"
    description = "Borra un archivo dentro del workspace (delicado: requiere aprobación humana)."
    risk = "high"

    def __init__(self, workspace: str | Path):
        self.runner = SandboxRunner(workspace)
        self.workspace = str(Path(workspace).resolve())
        self.timeout = 5.0
        self.permissions = {"workspace": self.workspace, "read": True, "write": True, "delete": True}
        self.limits = {}
        self.schema = {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "ruta relativa del archivo a borrar"}},
            "required": ["path"],
        }
        self.handler = self._remove

    async def _remove(self, args: dict) -> dict:
        path = str(args.get("path", ""))
        if not path or ".." in path:
            return {"ok": False, "error": "path inválido"}
        try:
            self.runner.resolve_in_workspace(path)
        except SandboxError as exc:
            return {"ok": False, "error": str(exc)}
        script = (
            "import json,sys,pathlib\n"
            "root=pathlib.Path(sys.argv[1]).resolve()\n"
            "p=pathlib.Path(sys.argv[2]).resolve()\n"
            "try:\n p.relative_to(root)\nexcept ValueError:\n"
            " print('ALEXIS_RESULT='+json.dumps({'ok':False,'error':'fuera de workspace'}));sys.exit(0)\n"
            "if p == root:\n"
            " print('ALEXIS_RESULT='+json.dumps({'ok':False,'error':'no se puede borrar el workspace'}));sys.exit(0)\n"
            "if not p.exists():\n"
            " print('ALEXIS_RESULT='+json.dumps({'ok':False,'error':'no existe: '+str(p)}));sys.exit(0)\n"
            "if p.is_dir():\n"
            " print('ALEXIS_RESULT='+json.dumps({'ok':False,'error':'solo borro archivos, no directorios'}));sys.exit(0)\n"
            "size=p.stat().st_size\n"
            "p.unlink()\n"
            "res={'ok':True,'path':str(p.relative_to(root)),'removed':True,'size':size}\n"
            "print('ALEXIS_RESULT='+json.dumps(res,ensure_ascii=False))"
        )
        result = await self.runner.run([sys.executable, "-c", script, self.workspace, str(Path(self.workspace) / path)])
        payload = _load_result(result.stdout)
        if result.timed_out or not result.ok:
            payload = {"ok": False, "error": payload.get("error") or "timeout o fallo de sandbox", "raw": result.stdout[:300]}
        payload["elapsed_ms"] = result.elapsed_ms
        return payload


def build_filesystem_tools(workspace: str | Path) -> list[Tool]:
    reader = FileSystemReadTool(workspace)
    stat = FileSystemStatTool(workspace)
    writer = FileSystemWriteTool(workspace)
    remove = FileSystemRemoveTool(workspace)
    tools = [
        Tool(
            name=reader.name,
            description=reader.description,
            risk=reader.risk,
            handler=reader.handler,
            schema=reader.schema,
            permissions=reader.permissions,
            timeout=reader.timeout,
            limits=reader.limits,
        ),
        Tool(
            name=stat.name,
            description=stat.description,
            risk=stat.risk,
            handler=stat.handler,
            schema=stat.schema,
            permissions=stat.permissions,
            timeout=stat.timeout,
            limits=stat.limits,
        ),
        Tool(
            name=writer.name,
            description=writer.description,
            risk=writer.risk,
            handler=writer.handler,
            schema=writer.schema,
            permissions=writer.permissions,
            timeout=writer.timeout,
            limits=writer.limits,
        ),
    ]
    tools.append(
        Tool(
            name=remove.name,
            description=remove.description,
            risk=remove.risk,
            handler=remove.handler,
            schema=remove.schema,
            permissions=remove.permissions,
            timeout=remove.timeout,
            limits=remove.limits,
        )
    )
    return tools