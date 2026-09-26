#!/usr/bin/env bash
# ALEXIS — túnel SSH del PC hacia la VPS (lo pesado: DB + Ollama).
#
# Abre dos túneles que hacen que los servicios del VPS aparezcan en los MISMOS
# puertos locales que antes (5433 y 11434); así NO cambia nada en tu config.
# Uso:  ./scripts/vps-tunnel.sh start USUARIO@IP_VPS
#       ./scripts/vps-tunnel.sh stop
#       ./scripts/vps-tunnel.sh status
set -euo pipefail

PIDFILE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/.vps-tunnel.pid"
L_DB=5433
L_OLLAMA=11434

start() {
  target="${1:?uso: vps-tunnel.sh start USUARIO@IP_VPS}"
  if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    echo "El túnel ya está abierto (pid $(cat "$PIDFILE"))."
    return 0
  fi
  ssh -fN -o ServerAliveInterval=30 -o ExitOnForwardFailure=yes \
    -L "$L_DB:127.0.0.1:$L_DB" \
    -L "$L_OLLAMA:127.0.0.1:$L_OLLAMA" \
    "$target"
  pgrep -f "ssh -fN.*$L_DB:127.0.0.1:$L_DB" | head -1 > "$PIDFILE"
  echo "Túneles abiertos: localhost:$L_DB -> DB del VPS, localhost:$L_OLLAMA -> Ollama del VPS."
}

stop() {
  if [ -f "$PIDFILE" ]; then
    kill "$(cat "$PIDFILE")" 2>/dev/null || true
    rm -f "$PIDFILE"
  fi
  pkill -f "$L_DB:127.0.0.1:$L_DB" 2>/dev/null || true
  echo "Túneles cerrados."
}

status() {
  if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    echo "activo (pid $(cat "$PIDFILE")). Prueba: curl -m 3 http://127.0.0.1:11434/api/tags"
  else
    echo "inactivo."
  fi
}

case "${1:-}" in
  start) start "${2:-}";;
  stop) stop;;
  status) status;;
  *) echo "uso: $0 {start USUARIO@IP_VPS | stop | status}"; exit 1;;
esac