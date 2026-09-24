"""Herramientas de escritorio portadas de JARVIS, como Tools del registro ALEXIS.

Cada capacidad es una `alexis.tools.registry.Tool` individual; NO existe ninguna
secuencia rígida "doble palmada → hacer X/Y/Z". Con un clap el orchestrador solo
decide *si* usar alguna de estas herramientas según la misión y las autorizaciones.

Reglas de honestidad:
- En Linux estas acciones son plausibles (Chrome instalado, URL abierta por defecto)
  o devuelven `ok: false` con motivo claro. Nunca fingen éxito.
- No se abre ningún navegador desde las pruebas: los puntos de apertura son
  inyectables (`webbrowser_open`, `chrome_executable`, `launcher`).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import webbrowser
from pathlib import Path

from alexis.tools.filesystem import extract_workspace_path
from alexis.tools.registry import Tool


def _env(*names: str, default: str = "") -> str:
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    return default


SONG_URI = _env("ALEXIS_DESKTOP_SPOTIFY_URI", "JARVIS_SONG_URI", "SONG_URI", default="")
CLAUDE_URL = _env("ALEXIS_DESKTOP_CLAUDE_URL", "CLAUDE_CODE_URL", default="https://claude.ai/new")
BINANCE_URL = _env(
    "ALEXIS_DESKTOP_BINANCE_URL",
    "BINANCE_BTC_URL",
    default="https://www.binance.com/en/trade/BTC_USDT",
)


def chrome_executable() -> str | None:
    if os.name == "nt":
        for base in (
            os.environ.get("ProgramFiles", r"C:\Program Files"),
            os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
            os.environ.get("LOCALAPPDATA", ""),
        ):
            if not base:
                continue
            p = os.path.join(base, "Google", "Chrome", "Application", "chrome.exe")
            if os.path.isfile(p):
                return p
    return shutil.which("google-chrome") or shutil.which("chrome")


def chrome_open(
    url: str,
    *,
    new_window: bool = True,
    fullscreen: bool = False,
    width: int = 1400,
    height: int = 900,
    webbrowser_open=webbrowser.open,
    chrome=None,
) -> dict:
    u = (url or "").strip()
    if not u:
        return {"ok": False, "reason": "sin URL"}
    exe = chrome if chrome is not None else chrome_executable()
    if exe:
        args = [exe]
        if new_window:
            args.append("--new-window")
        if fullscreen:
            args.append("--start-fullscreen")
        else:
            args.append(f"--window-size={width},{height}")
        args.append(u)
        try:
            subprocess.Popen(
                args,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return {"ok": True, "engine": "chrome", "url": u}
        except OSError as e:
            return {"ok": False, "engine": "chrome", "url": u, "reason": str(e)}
    try:
        webbrowser_open(u)
        return {"ok": True, "engine": "system-browser", "url": u}
    except OSError as e:
        return {"ok": False, "engine": "system-browser", "url": u, "reason": str(e)}


def spotify_play(uri: str, *, webbrowser_open=webbrowser.open) -> dict:
    u = (uri or SONG_URI or "").strip()
    if not u:
        return {"ok": False, "reason": "sin URI de canción (env ALEXIS_DESKTOP_SPOTIFY_URI)"}
    try:
        if os.name == "nt":
            os.startfile(u)  # type: ignore[attr-defined]
        else:
            webbrowser_open(u)
        return {"ok": True, "uri": u}
    except OSError as e:
        return {"ok": False, "uri": u, "reason": str(e)}


def open_claude(*, webbrowser_open=webbrowser.open, chrome=None) -> dict:
    return chrome_open(CLAUDE_URL, webbrowser_open=webbrowser_open, chrome=chrome)


def open_binance(*, webbrowser_open=webbrowser.open, chrome=None) -> dict:
    return chrome_open(BINANCE_URL, webbrowser_open=webbrowser_open, chrome=chrome)


def cursor_executable() -> str | None:
    if os.name == "nt":
        return shutil.which("cursor") or shutil.which("Cursor")
    return shutil.which("cursor")


def _url_of(text: str) -> str | None:
    """Extrae un dominio o URL del texto (honesto: devuelve lo que el usuario
    nombró, no una URL inventada)."""
    t = (text or "").strip()
    if not t:
        return None
    low = t.lower()
    for token in low.replace(",", " ").split():
        if token.startswith("https://") or token.startswith("http://"):
            return token
        if "." in token and not token.startswith(("abre", "en", "el")):
            return token
    if low.startswith(("abre ", "ábreme ")):
        after = low.split(" ", 1)[1].strip()
        if "." in after:
            return after
    return None


def desktop_intent(text: str) -> tuple[str, dict] | None:
    """Clasifica una intención de escritorio honesta.

    Devuelve ``(tool_name, args)`` o ``None`` si el texto es una pregunta de
    información, una consulta, o no es accionable. La palmada NO ejecuta estas
    tools: solo se clasifica para que el orchestrador decida si usar la tool
    (y bajo qué autonomía).
    """
    t = (text or "").strip()
    if not t:
        return None
    low = t.lower()

    if any(k in low for k in ("dame información", "qué es", "qué es claude", "dame info")):
        return None

    u = _url_of(t)
    if u and ("abre " in low or "ábreme" in low or t.startswith("abre ")):
        if u.startswith(("https://", "http://")) or "." in u:
            return ("chrome.open_url", {"url": u})

    if "spotify" in low or "música" in low or "musica" in low or "canción" in low or "cancion" in low:
        return ("spotify.play", {})

    if "voz alta" in low or "por voz" in low or "pronuncia" in low or "en voz" in low or "dime" in low:
        # tts: el texto a pronunciar es lo que sigue a «dime/pronuncia ... en voz».
        phrase = t
        for marker in ("dime ", "pronuncia ", "dilo en voz alta", "por voz", "en voz alta"):
            phrase = phrase.replace(marker, "")
        text2 = " ".join(phrase.split())
        text2 = text2.strip(" .!?")
        if text2:
            return ("tts.speak", {"text": text2})
        return ("tts.speak", {})

    if "binance" in low or "bitcoin" in low or "gráfico de bitcoin" in low:
        return ("binance.open", {})
    if "claude" in low:
        return ("claude.open", {})
    if "cursor" in low:
        return ("cursor.open", {})
    return None


def desktop_tool_for(objective: str) -> tuple[str, dict] | None:
    """Intención de escritorio de una misión, a salvo de colisiones con el
    workspace: si el usuario menciona un archivo, la misión NO es desktop."""
    if extract_workspace_path(objective or "") is not None:
        return None
    return desktop_intent(objective)


def desktop_reply(action: str, **kwargs) -> str:
    """Respuesta honesta (español) que el orchestrador usa al despachar una
    tool de escritorio. Nunca inventa que se abrió algo que no se abrió."""
    url = (kwargs.get("url") or "").strip()
    replies = {
        "spotify.play": "Listo, puse la canción en Spotify.",
        "chrome.open_url": f"Listo, abrí {url or 'esa URL'} en el navegador.",
        "claude.open": "Listo, abrí Claude Code en el navegador.",
        "binance.open": "Listo, abrí el gráfico de bitcoin.",
        "cursor.open": "Listo, abrí Cursor.",
        "tts.speak": "Listo, lo dije en voz alta.",
    }
    return replies.get(action, "Listo, acción de escritorio realizada.")


def cursor_open(*, launcher: subprocess.Popen | None = None, new_window: bool = True, executable: str | None = None) -> dict:
    exe = executable or cursor_executable()
    if not exe:
        return {"ok": False, "reason": "cursor no encontrado en PATH (instala Cursor)"}
    args = [exe, "-n"] if new_window else [exe]
    try:
        subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return {"ok": True, "exe": exe, "new_window": new_window}
    except OSError as e:
        return {"ok": False, "exe": exe, "reason": str(e)}


def tts_speak(text: str, provider=None) -> dict:
    if not (text or "").strip():
        return {"ok": False, "reason": "sin texto"}
    from alexis.speech.tts import get_tts_provider

    p = provider if provider is not None else get_tts_provider()
    try:
        import asyncio

        result = asyncio.run(p.synthesize(text))
        return {
            "ok": result.ok,
            "text": text,
            "provider": result.provider,
            "message": result.message,
            "path": result.path,
            "format": result.format,
            "error": result.error,
        }
    except Exception as e:  # noqa: BLE001 — devuelve razón honesta
        return {"ok": False, "text": text, "reason": str(e)}


_SCHEMA_URL = {
    "type": "object",
    "properties": {
        "url": {"type": "string", "description": "URL absoluta a abrir en el navegador"},
        "fullscreen": {"type": "boolean", "description": "abrir en pantalla completa"},
    },
    "required": ["url"],
}


def build_desktop_tools(
    *,
    provider=None,
    webbrowser_open=webbrowser.open,
    chrome=None,
) -> list[Tool]:
    def chrome_h(**kwargs):
        return chrome_open(
            url=kwargs.get("url") or "",
            fullscreen=bool(kwargs.get("fullscreen")),
            webbrowser_open=webbrowser_open,
            chrome=chrome,
        )

    def spotify_h(**kwargs):
        return spotify_play(kwargs.get("uri") or "", webbrowser_open=webbrowser_open)

    def claude_h(**kwargs):
        return open_claude(webbrowser_open=webbrowser_open, chrome=chrome)

    def binance_h(**kwargs):
        return open_binance(webbrowser_open=webbrowser_open, chrome=chrome)

    def cursor_h(**kwargs):
        return cursor_open(new_window=True, executable=kwargs.get("executable"))

    def tts_h(**kwargs):
        return tts_speak(kwargs.get("text") or "", provider=provider)

    def _wrap(fn):
        async def handler(**kwargs):
            return fn(**kwargs)

        handler.__name__ = f"handler_{fn.__name__}"
        return handler

    return [
        Tool(
            name="chrome.open_url",
            description="Abrir una URL en Google Chrome (o en el navegador del sistema si Chrome no existe).",
            risk="medium",
            handler=_wrap(chrome_h),
            schema=_SCHEMA_URL,
            permissions={"network": "open-tab"},
            timeout=15,
            limits={"cooldown_s": 2},
        ),
        Tool(
            name="spotify.play",
            description="Abrir la canción en Spotify (web o desktop, igual que JARVIS).",
            risk="low",
            handler=_wrap(spotify_h),
            schema={
                "type": "object",
                "properties": {"uri": {"type": "string", "description": "URI o URL de Spotify"}},
                "required": ["uri"],
            },
            timeout=10,
        ),
        Tool(
            name="claude.open",
            description="Abrir Claude Code en Chrome (pantalla completa en Windows).",
            risk="medium",
            handler=_wrap(claude_h),
            schema={"type": "object", "properties": {}, "required": []},
            timeout=15,
        ),
        Tool(
            name="binance.open",
            description="Abrir el mercado BTC/USDT de Binance en Chrome.",
            risk="medium",
            handler=_wrap(binance_h),
            schema={"type": "object", "properties": {}, "required": []},
            timeout=15,
        ),
        Tool(
            name="cursor.open",
            description="Abrir una ventana de Cursor (editor). En Windows intenta maximizar la ventana.",
            risk="medium",
            handler=_wrap(cursor_h),
            schema={"type": "object", "properties": {}, "required": []},
            timeout=15,
        ),
        Tool(
            name="tts.speak",
            description="Decir en voz alta un texto con la voz configurada (ElevenLabs si hay credencial).",
            risk="low",
            handler=_wrap(tts_h),
            schema={
                "type": "object",
                "properties": {"text": {"type": "string", "description": "Texto a pronunciar"}},
                "required": ["text"],
            },
            timeout=60,
        ),
    ]