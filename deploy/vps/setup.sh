#!/usr/bin/env bash
# ALEXIS — aprovisiona el stack pesado en el VPS (Postgres + Ollama [+ GPU].
#
# Uso (desde el repo ya copiado al VPS, p. ej. en /opt/alexis):
#   cd deploy/vps && ./setup.sh        # CPU
#   cd deploy/vps && ./setup.sh --gpu  # si el VPS tiene NVIDIA + toolkit
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"        # raíz del repo = lo que se monta en migrate
use_gpu=0
[ "${1:-}" = "--gpu" ] && use_gpu=1

echo "==> ALEXIS VPS setup (root= $ROOT)"

# 1) .env con credenciales
if [ ! -f "$HERE/.env" ]; then
  cp "$HERE/.env.example" "$HERE/.env"
  echo "    Se creó deploy/vps/.env con un password provisional."
  echo "    EDÍTALO antes de seguir (POSTGRES_PASSWORD)."
  read -r -p "    ¿Ya lo editaste? [y/N] " ok
  [ "${ok:-N}" = "y" ] || { echo "Cancelo."; exit 1; }
fi

# 2) preflight
command -v docker >/dev/null || { echo "ERROR: instala docker en el VPS."; exit 1; }
docker compose version >/dev/null 2>&1 || docker-compose version >/dev/null 2>&1 || {
  echo "ERROR: plugin docker compose no disponible."; exit 1; }

# 3) levantar postgres + ollama y esperar salud
export ALEXIS_ROOT="$ROOT"
compose=(docker compose -f "$HERE/docker-compose.yml")
if [ "$use_gpu" = "1" ]; then
  compose+=(-f "$HERE/docker-compose.gpu.yml")
  nvidia-smi >/dev/null 2>&1 || { echo "ERROR: --gpu pero nvidia-smi no existe."; exit 1; }
fi
"${compose[@]}" up -d postgres ollama
echo "==> Esperando healthchecks..."
for _ in $(seq 1 40); do
  ok=1
  for svc in postgres ollama; do
    st=$("${compose[@]}" ps -q "$svc" | xargs -r docker inspect -f '{{.State.Health.Status}}')
    [ "$st" = "healthy" ] || ok=0
  done
  [ "$ok" = "1" ] && break
  sleep 3
done
[ "$ok" = "1" ] || { echo "ERROR: servicios no sanos."; "${compose[@]}" ps; exit 1; }
echo "==> postgres y ollama sanos."

# 4) migrar esquema (extension vector + schema), one-shot
"${compose[@]}" run --rm migrate
"${compose[@]}" ps

# 5) modelos de voz (baja ambos; el activo lo decide tu PC con ALEXIS_MODEL_EXTRA_MODEL)
echo "==> bajando modelos (puede tardar)..."
for model in llama3.2:3b llama3.2:1b; do
  timeout 1800 docker exec "$("${compose[@]}" ps -q ollama)" ollama pull "$model" || true
done

cat <<'EOF'

==> LISTO en el VPS.
  - Postgres : 127.0.0.1:5433 (solo VPS; tu PC lo alcanza por túnel SSH)
  - Ollama   : 127.0.0.1:11434 (idem)
  - GPU      : [CPU / NVIDIA] según hayas elegido

En TU PC, abre los túneles (idénticos puertos -> cero cambios de config):
  ssh -N -L 5433:127.0.0.1:5433 -L 11434:127.0.0.1:11434 USUARIO@IP_VPS
  (o usa scripts/vps-tunnel.sh)

Vuelve a bajar/modelo en tu PC si quieres máxima calidad local ahora que hay VPS:
  ALEXIS_MODEL_EXTRA_MODEL=llama3.2:3b   (en secrets/models.env)
EOF