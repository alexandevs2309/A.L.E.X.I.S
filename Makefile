PYTHON ?= python3
VENV_PYTHON ?= .venv/bin/python

run:
	uvicorn apps.api.main:app --reload --host 127.0.0.1 --port 8000

test:
	pytest -q

# R1: falla si hay credenciales en texto plano en archivos versionables.
secrets-check:
	$(VENV_PYTHON) scripts/check_secrets.py

# Instala el hook de commit sin dependencias (no necesita pre-commit instalado).
# Con `make secrets-check` el hook escanea el árbol entero y falla si tienes credenciales
# reales en secrets/ (que es lo correcto para una auditoría, pero hace que el hook sea
# inútil día a día). El hook sólo mira lo que se va a commitear: tus secretos locales no
# se commitean, así que no se miran, y un secreto nuevo sí se detecta.
# Ver docs/DEVELOPMENT.md §7.3.
hooks-install:
	@mkdir -p .git/hooks
	@printf '%s\n' \
		'#!/usr/bin/env bash' \
		'# Instalado por \`make hooks-install\`. No editar aquí: está en el Makefile.' \
		'set -euo pipefail' \
		'cd "$(git rev-parse --show-toplevel)"' \
		'staged=$$(git diff --cached --name-only --diff-filter=ACM)' \
		'if [ -z "$$staged" ]; then exit 0; fi' \
		'python3 scripts/check_secrets.py $$staged' \
		> .git/hooks/pre-commit
	@chmod +x .git/hooks/pre-commit
	@echo "hook instalado en .git/hooks/pre-commit" 

# R4/R6: recordatorio de que compose exige POSTGRES_PASSWORD y ALEXIS_API_TOKEN.
env-check:
	@test -f .env || (echo "Falta .env. Copia .env.example a .env y define POSTGRES_PASSWORD y ALEXIS_API_TOKEN." && exit 1)
	@grep -q '^POSTGRES_PASSWORD=..*' .env || (echo "POSTGRES_PASSWORD vacio en .env" && exit 1)
	@grep -q '^ALEXIS_API_TOKEN=..*' .env || (echo "ALEXIS_API_TOKEN vacio en .env" && exit 1)
	@echo "env-check: OK"

precommit: secrets-check test

# Gate completo antes de commitear o publicar.
check: secrets-check env-check test
