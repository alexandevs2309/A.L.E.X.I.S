# Despliegue de ALEXIS en un VPS (solo lo pesado)

Un VPS con CPU capaz (y opcionalmente GPU NVIDIA) quita las dos limitaciones de la
máquina local: inferencia local lenta y respuestas cortadas. Aquí se despliega la
parte cara (**Postgres + Ollama**); tu PC sigue siendo la interfaz (voz, escritorio,
web) conectándose por túneles SSH a los **mismos puertos locales**: cambio cero en
tu configuración.

## Qué corre dónde

| Pieza | Local (tu PC) | VPS |
|---|---|---|
| Interfaz web/voz (demo :8100) | x | |
| Micrófono / altavoces / tools de escritorio | x | |
| Inferencia del modelo (Ollama) | (fallback) | x |
| Base de datos Postgres (pgvector) | | x |
| Gemini/API cloud (opcional) | x | |

## En el VPS (una sola vez)

```bash
# 1) Requisitos: docker + plugin compose (+ opcional nvidia-container-toolkit)
# 2) Copiar el repo (usa rsync para llevar el código de TU PC sin subir a GitHub):
rsync -av --delete --exclude=.git --exclude=secrets --exclude=.cache \
  --exclude=workspace --exclude=node_modules ./ usuario@IP_VPS:/opt/alexis/

# 3) Aprovisionar
ssh usuario@IP_VPS
cd /opt/alexis/deploy/vps
./setup.sh            # CPU
#  ./setup.sh --gpu   # si hay NVIDIA + toolkit
```

El `setup.sh` crea `deploy/vps/.env`, levanta postgres + ollama, espera healthchecks,
ejecuta la migración del esquema (one-shot) y baja los modelos `llama3.2:3b` y `1b`.

## En tu PC (después de cada reinicio de sesión SSH)

```bash
./scripts/vps-tunnel.sh start usuario@IP_VPS   # abre :5433 y :11434 hacia la VPS
./scripts/vps-tunnel.sh status
curl -m 3 http://127.0.0.1:11434/api/tags      # debe responder = Ollama del VPS
```

Probar que la demo sigue igual:
`./alexis.sh start` (o la demo que uses).

## Subir la calidad ahora que hay VPS (opcional)

En `secrets/models.env`, elige modelo más capaz (el VPS lo corre rápido):

```
ALEXIS_MODEL_EXTRA_MODEL=llama3.2:3b
```

y reinicia la demo. Si cambias de idea: `llama3.2:1b`.

## Notas de seguridad

- postgres y ollama se publican solo en `127.0.0.1` del VPS: no hay puerto abierto
  al exterior; el único acceso es el túnel SSH (cifrado).
- Ollama no trae autenticación: por eso nunca se expone a `0.0.0.0`.
- `POSTGRES_PASSWORD` vive en `deploy/vps/.env` (gitignored), jamás en el repo.
- El contenedor `migrate` es one-shot: crea la extensión `vector` y el schema, y termina.

## Operación

```bash
cd /opt/alexis/deploy/vps && ./setup.sh   # re-ejecutar migra/sube si cambió el schema
# o directamente:
docker compose -f docker-compose.yml ps
docker compose -f docker-compose.yml logs -f ollama
```

Para GPU: `docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d ollama`