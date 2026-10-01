"""CORE-03 — Lanzador del runtime OFICIAL de ALEXIS.

Este módulo ya no es "el runtime": es el proceso que levanta la aplicación oficial
definida en `apps/demo/app.py`. Toda la construcción (CognitiveRuntime, GoalVerifier,
AlexisRuntime, EventBus, catálogo, política, gate, sandbox, catálogo de tools, mundo,
Self Model, ModelRouter, IntentClassifier y ConversationSession) vive allí, y ocurre en
un único sitio. Aquí no se instancia nada de eso: se conecta y se enciende.

Lo que queda aquí, y sólo aquí, son los efectos que no son de la aplicación:

- el event loop en un hilo y la apertura de PostgreSQL,
- el worker de la cola y la recuperación de misiones abiertas al arrancar,
- la PRESENTACIÓN del demo: avatar/face, modo voz y la escucha de palmadas,
- el arranque del servidor HTTP sobre la superficie oficial.

La razón de separar esto es que `app.py` tiene que ser importable para poder testearlo y
para que `apps/api` lo reutilice. Antes, `server.py` componía la app entera al
importarse: eso hacía imposible importar la aplicación en un test y obligaba a que los
tests comprobaran el cableado leyendo el fuente como texto.

Ejecutar:  python3 -m apps.demo.server
"""

import asyncio
import json
import os
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

from apps.demo.app import (
    OfficialRuntime,
    build_runtime as build_runtime_from_app,
    create_handler,
    init_storage,
    start_services,
)
from apps.ui import PAGE
from alexis.contracts import AutonomyLevel, MissionEnvelope, MissionState
from alexis.perception.activation import ACTIVATION_OBJECTIVE
from alexis.perception.clap_listener import DEFAULT_TOPIC, ClapListener
from alexis.speech.tts import get_tts_provider, synthesize_with_fallback

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FACE = os.path.join(_PROJECT_ROOT, "1000560050.jpg")
FACE_DIST = os.path.join(_PROJECT_ROOT, "apps", "face", "dist")

# --- Modo voz (palmada = trigger, estilo ChatGPT). --------------------------
# Es una SESIÓN: la palmada lo ACTIVA y ALEXIS habla mientras haya interacción de
# voz/mic; si no hay actividad durante _VOICE_IDLE_S vuelve solo a texto.
_VOICE_MODE_DEFAULT = os.environ.get("ALEXIS_VOICE_MODE_DEFAULT", "0").strip().lower() in ("1", "on", "true", "yes")
_VOICE_IDLE_S = float(os.environ.get("ALEXIS_VOICE_IDLE_S", "30"))


def _voice_dir() -> Path:
    override_cfg = (os.environ.get("ALEXIS_TTS_CACHE_DIR") or os.environ.get("JARVIS_WELCOME_CACHE_DIR") or "").strip()
    if override_cfg:
        return Path(override_cfg).expanduser().resolve()
    return Path(_PROJECT_ROOT) / ".cache" / "tts"


def _touch_voice(rt: OfficialRuntime) -> None:
    rt.state["voice_last_activity"] = time.time()


def voice_mode_on(rt: OfficialRuntime) -> bool:
    """Estado REAL del modo voz; se apaga solo tras _VOICE_IDLE_S sin actividad."""
    if not rt.state.get("voice_mode", _VOICE_MODE_DEFAULT):
        return False
    if time.time() - rt.state.get("voice_last_activity", 0.0) > _VOICE_IDLE_S:
        rt.state["voice_mode"] = False
        asyncio.run_coroutine_threadsafe(
            rt.events.publish("voice.mode", {"enabled": False, "reason": "idle"}), rt.loop
        )
        return False
    return True


def set_voice_mode(rt: OfficialRuntime, enabled: bool) -> dict:
    rt.state["voice_mode"] = bool(enabled)
    if enabled:
        _touch_voice(rt)
    asyncio.run_coroutine_threadsafe(
        rt.events.publish("voice.mode", {"enabled": rt.state["voice_mode"], "reason": "manual"}), rt.loop
    )
    return {"enabled": rt.state["voice_mode"]}


def _publish_voice(path: str, mission_id: str) -> bool:
    """Copia el audio sintetizado con nombre ÚNICO en la carpeta que vigila el
    `voice_bridge` del host: así se reproduce por los altavoces aunque el mismo
    texto ya se haya dicho antes."""
    src = Path(path)
    if not src.is_file():
        return False
    dst_dir = _voice_dir()
    dst_dir.mkdir(parents=True, exist_ok=True)
    ext = (src.suffix or ".wav").lstrip(".")
    dst = dst_dir / f"{mission_id}-{int(time.time() * 1000)}.{ext}"
    try:
        import shutil

        shutil.copy2(src, dst)
        return True
    except OSError:
        return False


async def _announce_voice(rt: OfficialRuntime, mission) -> None:
    """Hace hablar a ALEXIS al terminar la misión (host altavoces vía bridge)."""
    if mission.state not in (MissionState.COMPLETED, MissionState.FAILED, MissionState.BLOCKED):
        return
    if not voice_mode_on(rt):
        return
    for res in reversed(mission.results or []):
        out = res.get("output") or {}
        if isinstance(out, dict) and out.get("action") == "respond":
            tts = out.get("tts") or {}
            if tts.get("ok") and tts.get("path"):
                _publish_voice(str(tts.get("path")), mission.id)
            return
    objective = mission.goal.objective
    if mission.state is MissionState.COMPLETED:
        text = f"Listo, completé la misión: {objective}."
    elif mission.state is MissionState.FAILED:
        text = "No pude completar esa misión. Revisa el objetivo y vuelve a intentarlo."
    else:
        text = "No logré confirmar el resultado. Dame más contexto o un objetivo más claro."
    asyncio.run_coroutine_threadsafe(rt.events.publish("presence.speaking", {"text": text}), rt.loop)
    tts_result = await synthesize_with_fallback(text, provider=get_tts_provider())
    if tts_result.ok and tts_result.path:
        _publish_voice(str(tts_result.path), mission.id)
    asyncio.run_coroutine_threadsafe(rt.events.publish("presence.idle", {}), rt.loop)


class DemoPresentation:
    """Rutas del demo (avatar/face/clap/voz) sobre la aplicación oficial.

    Se inyectan en el handler oficial en lugar de mezclarse con él: así la superficie
    oficial —`/chat`, `/missions`, `/stream`— no depende de que el demo exista, y el
    demo no puede redefinir esas rutas.
    """

    def __init__(self, rt: OfficialRuntime):
        self.rt = rt

    # -- utilidades ------------------------------------------------------- #
    def _send_file(self, handler, abspath: str):
        if not os.path.isfile(abspath):
            handler._send_json({"error": "not found"}, 404)
            return
        ctype, _ = __import__("mimetypes").guess_type(abspath)
        with open(abspath, "rb") as f:
            body = f.read()
        handler.send_response(200)
        handler.send_header("Content-Type", ctype or "application/octet-stream")
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Cache-Control", "no-cache")
        handler.end_headers()
        handler.wfile.write(body)

    def _face_index(self, handler):
        self._send_file(handler, os.path.join(FACE_DIST, "index.html"))

    # -- GET -------------------------------------------------------------- #
    def try_get(self, handler, path: str) -> bool:
        if path in ("/", "/avatar"):
            if os.path.isdir(FACE_DIST):
                self._face_index(handler)
            else:
                handler._send_json(
                    {"error": "face dist no construido (npm run build en apps/face)"}, 503
                )
            return True
        if path == "/classic":
            handler._send_html(PAGE)
            return True
        if path.startswith("/face/"):
            rel = path[len("/face/"):]
            if not rel:
                self._face_index(handler)
            else:
                self._send_file(handler, os.path.join(FACE_DIST, rel))
            return True
        if path == "/face.jpg":
            if os.path.exists(FACE):
                with open(FACE, "rb") as f:
                    body = f.read()
                handler.send_response(200)
                handler.send_header("Content-Type", "image/jpeg")
                handler.send_header("Content-Length", str(len(body)))
                handler.end_headers()
                handler.wfile.write(body)
            else:
                handler._send_json({"error": "face image not found"}, 404)
            return True
        if path == "/voice-mode":
            handler._send_json({"enabled": voice_mode_on(self.rt)})
            return True
        return False

    # -- POST ------------------------------------------------------------- #
    def try_post(self, handler, path: str) -> bool:
        if path == "/voice-mode":
            length = int(handler.headers.get("Content-Length", 0))
            data = json.loads(handler.rfile.read(length) or b"{}")
            handler._send_json(
                set_voice_mode(self.rt, bool(data.get("enabled", voice_mode_on(self.rt))))
            )
            return True
        if path == "/clap":
            on_clap_event(self.rt, {"source": "simulated"})
            handler._send_json({
                "ok": True,
                "note": "clap simulated - modo voz activado y pipeline de activación disparado",
            })
            return True
        return False


def on_clap_event(rt: OfficialRuntime, event) -> None:
    """La palmada es sólo una FUENTE de eventos: se convierte en una misión de
    activación que atraviesa el MISMO pipeline que el chat."""
    set_voice_mode(rt, True)
    now = time.time()
    if now - rt.state.get("last_clap_at", 0.0) < ACTIVATION_COOLDOWN_S:
        return
    rt.state["last_clap_at"] = now
    asyncio.run_coroutine_threadsafe(
        rt.events.publish("presence.listening", {"source": "clap"}), rt.loop
    )
    mission = rt.running.get(rt.state.get("mission_id"))
    if mission is not None and mission.state.value in ("planning", "running", "verifying"):
        return
    envelope = MissionEnvelope(
        objective=ACTIVATION_OBJECTIVE,
        autonomy=AutonomyLevel.SUPERVISED,
        allowed_actions=[
            "understand", "analyze", "research", "execute", "verify",
            "modify", "test", "commit", "write", "respond",
        ],
        capabilities=rt.enabled_capabilities,
    )
    mission = rt.missions.create(ACTIVATION_OBJECTIVE, envelope)
    rt.running[mission.id] = mission
    rt.state["mission_id"] = mission.id
    rt.enqueue_or_run(mission)


ACTIVATION_COOLDOWN_S = 10.0


def build_runtime() -> OfficialRuntime:
    """Construye e INSTALA el runtime oficial de este proceso.

    Es el ÚNICO punto desde el que un proceso obtiene un ALEXIS en marcha. `app.py` es
    el único sitio donde la construcción ocurre, y este lanzador es el único que puede
    pedirla: `apps/api` no puede, y por eso no puede ser un segundo ALEXIS.
    """
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    storage = init_storage(loop)
    rt = build_runtime_from_app(
        loop=loop,
        storage=storage,
        voice_mode_provider=lambda: voice_mode_on(rt) if rt is not None else False,
        on_mission_end=lambda mission: _announce_voice(rt, mission),
        on_mission_activity=lambda: _touch_voice(rt),
    )
    rt.state["voice_mode"] = _VOICE_MODE_DEFAULT
    _touch_voice(rt)
    return rt


def main() -> None:
    rt = build_runtime()

    # Los servicios del runtime oficial se encienden por el MISMO camino que usa el
    # E2E (`start_services`): el arranque bajo prueba tiene que ser el de producción.
    start_services(rt)

    # Percepción del demo: la palmada es una fuente de eventos más, no un camino de
    # ejecución. Vive aquí, y no en `start_services`, porque depende de la voz y de la
    # activación —que son de este lanzador— y moverlo dentro de la aplicación oficial
    # volvería a meter la demo en el runtime único (lo contrario de CORE-03).
    clap = ClapListener()
    clap_sub = rt.events.subscribe_async()

    async def _clap_consumer():
        while True:
            item = await clap_sub.get()
            if item["topic"] == DEFAULT_TOPIC:
                on_clap_event(rt, item["payload"])

    asyncio.run_coroutine_threadsafe(_clap_consumer(), rt.loop)
    if os.environ.get("ALEXIS_CLAP_ENABLED", "1") == "1":
        ok, note = clap.start(rt.events, DEFAULT_TOPIC, rt.loop)
        print(f"[clap] listener: {note}")

    port = int(os.environ.get("ALEXIS_DEMO_PORT", "8100"))
    handler = create_handler(rt, presentation=DemoPresentation(rt))
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    print(f"ALEXIS (runtime oficial) en http://127.0.0.1:{port}")
    print("  rutas oficiales: /health /ui /chat /missions /stream /state /self /capabilities")
    print("  rutas del demo:  / /avatar /face /classic /voice-mode /clap")
    server.serve_forever()


if __name__ == "__main__":
    main()
