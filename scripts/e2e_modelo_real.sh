#!/usr/bin/env bash
# Levanta el demo de ALEXIS con un provider de modelo REAL (Gemini) y ejecuta turnos
# de prueba, guardando el stream de eventos para auditar la procedencia (P1).
#
# Uso:  scripts/e2e_modelo_real.sh [puerto]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${1:-8117}"
cd "$ROOT"

if [ ! -f secrets/gemini.env ]; then
  echo "Falta secrets/gemini.env (cópialo de secrets/gemini.env.example)" >&2
  exit 1
fi
set -a; . secrets/gemini.env; set +a

export ALEXIS_MODEL_PROVIDER="${ALEXIS_MODEL_PROVIDER:-openai_compatible}"
export ALEXIS_MODEL_BASE_URL="${ALEXIS_MODEL_BASE_URL:-https://generativelanguage.googleapis.com}"
export ALEXIS_MODEL_ENDPOINT="${ALEXIS_MODEL_ENDPOINT:-/v1beta/openai/chat/completions}"
export ALEXIS_MODEL_NAME="${ALEXIS_MODEL_NAME:-gemini-3.5-flash}"
export ALEXIS_MODEL_DIALECT=openai
export ALEXIS_MODEL_MAX_TOKENS="${ALEXIS_MODEL_MAX_TOKENS:-2048}"
export ALEXIS_MODEL_TIMEOUT_S="${ALEXIS_MODEL_TIMEOUT_S:-180}"
export ALEXIS_MODEL_DEADLINE_MS="${ALEXIS_MODEL_DEADLINE_MS:-180000}"
export ALEXIS_DEMO_PORT="$PORT"
export ALEXIS_CLAP_ENABLED=0

echo "== Arrancando demo en :$PORT con provider=$ALEXIS_MODEL_PROVIDER model=$ALEXIS_MODEL_NAME"
.venv/bin/python -m apps.demo.server > "/tmp/opencode/e2e-$PORT.log" 2>&1 &
DEMO_PID=$!
trap 'kill $DEMO_PID 2>/dev/null || true' EXIT

for _ in $(seq 1 40); do
  if curl -s --max-time 2 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then break; fi
  sleep 1
done

# Escucha el stream en segundo plano y guarda cada evento SSE.
( timeout 300 curl -sN "http://127.0.0.1:$PORT/stream" > "/tmp/opencode/e2e-stream-$PORT.log" 2>&1 ) &
STREAM_PID=$!
sleep 2

turn() {
  echo
  echo "== TURNO: $1"
  curl -s --max-time 240 -X POST "http://127.0.0.1:$PORT/chat" \
    -H 'Content-Type: application/json' \
    -d "{\"text\": $2}" | .venv/bin/python -c "
import json,sys
d=json.load(sys.stdin)
print('  respuesta:', d.get('text','')[:180].replace(chr(10),' '))
print('  cognition_outcome:', d.get('cognition_outcome'), '| degraded:', d.get('degraded'), '| mission_id:', d.get('mission_id'))
"
  sleep 3
}

turn "saludo" '"Hola ALEXIS."'
turn "capacidades" '"¿Qué puedes hacer?"'
turn "revision de proyecto" '"ALEXIS, revisa este proyecto y dime qué problemas importantes encuentras."'

sleep 8
kill $STREAM_PID 2>/dev/null || true

echo
echo "== EVENTOS model.routed / cognition.* del /stream =="
grep -oE '"topic": "(model\.routed|cognition\.[a-z_]+|conversation\.[a-z_]+)".*' "/tmp/opencode/e2e-stream-$PORT.log" \
  | .venv/bin/python -c "
import json,sys
for line in sys.stdin:
    topic=line.split('\"topic\": \"')[1].split('\"')[0]
    body=line[line.index('{'):]
    try: d=json.loads(body)
    except Exception: continue
    p=d.get('payload',{})
    extra=''
    if topic=='model.routed':
        extra=f\"provider={p.get('provider')} outcome={p.get('outcome')} model={p.get('model')} latency={p.get('latency_ms')}ms\"
    else:
        extra=json.dumps(p, ensure_ascii=False)[:150]
    print(f'  {topic}: {extra}')
"
echo
echo "== Log del demo (modelo) =="
grep -E "\[model\]|provider" "/tmp/opencode/e2e-$PORT.log" | head -5
