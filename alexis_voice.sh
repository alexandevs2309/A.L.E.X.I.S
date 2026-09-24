#!/usr/bin/env bash
# ALEXIS por VOZ en el HOST: palmada → misión → respuesta por los altavoces.
#
# Esto corre ALEXIS en tu máquina (no en contenedor) porque el micrófono y los
# altavoces viven aquí. Levanta dos procesos:
#   1. demo server  (apps.demo.server)  — escucha la palmada (sounddevice o
#      arecord/ALSA) y ejecuta las misiones (chat y dictado por voz).
#   2. voice_bridge (alexis.perception.voice_bridge) — reproduce en los
#      altavoces cada audio nuevo que ALEXIS sintetiza en .cache/tts.
#
# Uso:
#   ./alexis_voice.sh start     levanta demo + bridge de voz (host)
#   ./alexis_voice.sh stop      apaga ambos
#   ./alexis_voice.sh status    muestra el estado
#   ./alexis_voice.sh logs      sigue el log del demo
#   ./alexis_voice.sh voice     sigue el log del bridge de voz
#
# Si el puerto está ocupado por los contenedores, apágalos con ./alexis.sh stop
# o cambia el puerto: ALEXIS_DEMO_PORT=8200 ./alexis_voice.sh start
#
# El mic interno (ALC233/HDA) suele venir saturado por defecto; este script
# normaliza la ganancia de captura y usa el ADC explícito plughw (no "default",
# que puede leer un stream compartido viejo y enmascarar las palmadas).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || PY=python3
PORT="${ALEXIS_DEMO_PORT:-8100}"
URL="http://127.0.0.1:${PORT}"
PIDS="$ROOT/.alexis_host.pids"
export ALEXIS_INPUT_DEVICE="${ALEXIS_INPUT_DEVICE:-plughw:CARD=PCH,DEV=0}"
mkdir -p logs .cache/tts

# Ganancia de captura: sin "Capture" atenuado ni boost, el ADC satura (RMS≈1.0)
# y el detector nunca se rearma → las palmadas quedan enmascaradas.
amixer -c 0 sset Capture 20% >/dev/null 2>&1 || true
amixer -c 0 sset "Internal Mic Boost" 0 >/dev/null 2>&1 || true

# Cargar las credenciales de ElevenLabs si existen (opcional; sin ellas usa edge-tts).
if [ -f secrets/elevenlabs.env ]; then
  set -a
  # shellcheck disable=SC1091
  . secrets/elevenlabs.env
  set +a
fi

start() {
  if [ -f "$PIDS" ] && [ -s "$PIDS" ]; then
    echo "ALEXIS ya está corriendo en el host (pids: $(tr '\n' ' ' <"$PIDS")). Detenlo con ./alexis_voice.sh stop"
    return 1
  fi
  : >"$PIDS"
  setsid nohup "$PY" -u -m alexis.perception.voice_bridge --poll 0.4 >logs/voice_bridge.log 2>&1 &
  echo $! >>"$PIDS"
  echo "* voice_bridge pid $!"
  setsid nohup "$PY" -u -m apps.demo.server >logs/demo.log 2>&1 &
  echo $! >>"$PIDS"
  echo "* demo server    pid $!"
  sleep 1
  echo "ALEXIS por voz en el host → $URL  (/clap simula una palmada, /classic y /avatar para el chat y dictado)"
  echo "Logs: tail -f logs/demo.log    voz: tail -f logs/voice_bridge.log"
  echo "Para apagar: ./alexis_voice.sh stop"
}

stop() {
  if [ -f "$PIDS" ]; then
    while IFS= read -r pid; do
      [ -n "$pid" ] && kill -- "-$pid" >/dev/null 2>&1 || true
      [ -n "$pid" ] && kill "$pid" >/dev/null 2>&1 || true
    done <"$PIDS"
    : >"$PIDS"
  fi
  # Recolecta `arecord` huérfanos de reinicios anteriores (matar el python no
  # mata al hijo; sin esto, el mic queda ocupado y "default" lee un stream viejo).
  pkill -9 -x arecord >/dev/null 2>&1 || true
  sleep 0.3
  echo "ALEXIS (host) apagado."
}

status() {
  if [ -f "$PIDS" ] && [ -s "$PIDS" ]; then
    echo "procesos: $(tr '\n' ' ' <"$PIDS")"
  else
    echo "no corriendo (host)"
  fi
  code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 2 "$URL" 2>/dev/null || true)
  echo "demo: $code"
}

case "${1:-start}" in
  start|up)   start ;;
  stop|down)  stop ;;
  restart)    stop; start ;;
  status)     status ;;
  clap)       tail -f logs/demo.log | grep --line-buffered "\[clap\]" ;;
  logs)       tail -f logs/demo.log ;;
  voice)      tail -f logs/voice_bridge.log ;;
  *) echo "Uso: $0 [start|stop|restart|status|clap|logs|voice]" ;;
esac