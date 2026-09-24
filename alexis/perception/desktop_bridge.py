"""Acciones de escritorio en el HOST (el contenedor no tiene GUI).

El cerebro (contenedor) decide la intención con `desktop_intent` y la ordena
(dispatching a "host"). Este proceso, en el host con escritorio, vigila /state
y ejecuta la acción real una vez por misión completada:
  claude.open / binance.open / chrome.open_url → navegador (webbrowser)
  spotify.play → URI configurada (ALEXIS_DESKTOP_SPOTIFY_URI)
  cursor.open → binario cursor
No ejecuta NADA solo: solo órdenes que ya pasaron por el orchestrador.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import alexis.tools.desktop as desk

PROJECT = Path(__file__).resolve().parent.parent.parent
MEMO = PROJECT / "alexis" / ".desktop_bridge_done.json"


def _fetch_state(url: str) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return json.load(r)
    except Exception:  # noqa: BLE001 — el demo puede no estar arriba
        return None


def _perform(tool_name: str, args: dict) -> dict:
    if tool_name == "claude.open":
        return desk.open_claude()
    if tool_name == "binance.open":
        return desk.open_binance()
    if tool_name == "chrome.open_url":
        return desk.chrome_open(args.get("url") or "")
    if tool_name == "spotify.play":
        import os

        uri = args.get("uri") or os.environ.get("ALEXIS_DESKTOP_SPOTIFY_URI", "") or desk.SONG_URI
        return desk.spotify_play(uri)
    if tool_name == "cursor.open":
        return desk.cursor_open()
    return {"ok": False, "reason": f"tool desconocida {tool_name}"}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Ejecuta en el host las órdenes de escritorio que decide ALEXIS")
    ap.add_argument("--url", default="http://127.0.0.1:8100/state")
    ap.add_argument("--poll", type=float, default=1.0)
    args = ap.parse_args(argv)

    done: set[str] = set()
    if MEMO.exists():
        try:
            done = set(json.loads(MEMO.read_text()))
        except json.JSONDecodeError:
            done = set()
    print(f"[desktop] vigilando {args.url} (ejecuta en el host las órdenes aprobadas). Ctrl+C para salir.")
    try:
        while True:
            state = _fetch_state(args.url)
            mission = (state or {}).get("mission")
            if mission and mission.get("state") == "completed" and mission.get("id") not in done:
                mid = mission.get("id")
                done.add(mid)
                MEMO.write_text(json.dumps(sorted(done), ensure_ascii=False))
                intent = desk.desktop_intent(mission.get("objective") or "")
                if intent:
                    tool_name, args_out = intent
                    result = _perform(tool_name, args_out)
                    status = "ejecuté" if result.get("ok") else "no pude ejecutar"
                    print(f"[desktop] misión {mid}: {tool_name} → {status} {result}")
            time.sleep(args.poll)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())