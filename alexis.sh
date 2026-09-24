#!/usr/bin/env bash
# ALEXIS — control del entorno de desarrollo (un solo comando para todo).
#
# Uso:
#   ./alexis.sh start     arranca PostgreSQL y el demo (orden correcto)
#   ./alexis.sh stop      apaga demo y PostgreSQL
#   ./alexis.sh restart   reinicia ambos
#   ./alexis.sh status    muestra estado
#   ./alexis.sh logs      sigue los logs del demo
#
# Nota: si los contenedores no existen, te dice cómo crear el primer `alexis-demo`;
# PostgreSQL se crea con: docker run -d --name alexis-pg-a1 -p 127.0.0.1:5433:5432 \
#   -e POSTGRES_DB=alexis -e POSTGRES_USER=alexis -e POSTGRES_PASSWORD="$POSTGRES_PASSWORD" \
#   pgvector/pgvector:pg16
#
# R1: la credencial de ElevenLabs NO se versiona. Crea el archivo real desde la
# plantilla y mantenlo fuera de git/ZIP:
#   cp secrets/elevenlabs.env.example secrets/elevenlabs.env && $EDITOR secrets/elevenlabs.env
# Verifica antes de commitear: make secrets-check
set -euo pipefail

PG="alexis-pg-a1"
DEMO="alexis-demo"
URL="http://127.0.0.1:${ALEXIS_DEMO_PORT:-8100}"

exists() { docker ps -a --format "{{.Names}}" | grep -qx "$1"; }

up() {
  if ! exists "$PG"; then
    echo "✗ Falta el contenedor '$PG'. Créalo con:"
    echo "  docker run -d --name alexis-pg-a1 -p 127.0.0.1:5433:5432 -e POSTGRES_DB=alexis -e POSTGRES_USER=alexis -e POSTGRES_PASSWORD=\"\$POSTGRES_PASSWORD\" pgvector/pgvector:pg16"
    echo "  (POSTGRES_PASSWORD sin default: defínelo en tu entorno o en .env)"
    return 1
  fi
  if ! exists "$DEMO"; then
    echo "✗ Falta el contenedor '$DEMO'. Créalo desde la raíz del proyecto con:"
    echo "  docker run -d --name alexis-demo --restart unless-stopped --network host -v \"\$PWD\":/app -w /app --env-file ./secrets/elevenlabs.env -e PYTHONPATH=/app python:3.12-slim bash -c 'pip install \"numpy>=1.24,<3\" psycopg[binary] psycopg_pool edge-tts elevenlabs && python3 -m apps.demo.server'"
    echo "  (--env-file lleva la key de ElevenLabs; créalo antes con: cp secrets/elevenlabs.env.example secrets/elevenlabs.env — si no lo tienes, omite el flag.)"
    return 1
  fi
  docker start "$PG" >/dev/null
  echo "✓ PostgreSQL ($PG)"
  docker start "$DEMO" >/dev/null
  echo "✓ Demo ($DEMO)"
  echo "  ALEXIS en: $URL   (/   /classic   /avatar   /state   /missions)"
  return 0
}

stop() {
  docker stop "$DEMO" >/dev/null 2>&1 || true
  docker stop "$PG" >/dev/null 2>&1 || true
  echo "✓ Apagados ($DEMO, $PG)"
}

status() {
  docker ps -a --filter "name=^($PG|$DEMO)$" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
  echo -n "Esperando demo…"
  code="off"
  for _ in $(seq 1 20); do
    code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 2 "$URL" 2>/dev/null || true)
    if [ "$code" = "200" ]; then break; fi
    sleep 1; echo -n "."
  done
  echo " → $code"
}

case "${1:-start}" in
  start|up)   up ;;
  stop|down)  stop ;;
  restart)    stop; up ;;
  status)     status ;;
  logs)       docker logs -f "$DEMO" ;;
  trace)      docker inspect --format '{{.State.Running}} {{.State.Status}}' "$DEMO" ;;
  *) echo "Uso: $0 [start|stop|restart|status|logs]" ;;
esac