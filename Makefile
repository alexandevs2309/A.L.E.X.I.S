PYTHON ?= python3
VENV_PYTHON ?= .venv/bin/python

run:
	uvicorn apps.api.main:app --reload --host 127.0.0.1 --port 8000

test:
	pytest -q

# R1: falla si hay credenciales en texto plano en archivos versionables.
secrets-check:
	$(VENV_PYTHON) scripts/check_secrets.py

# R4/R6: recordatorio de que compose exige POSTGRES_PASSWORD y ALEXIS_API_TOKEN.
env-check:
	@test -f .env || (echo "Falta .env. Copia .env.example a .env y define POSTGRES_PASSWORD y ALEXIS_API_TOKEN." && exit 1)
	@grep -q '^POSTGRES_PASSWORD=..*' .env || (echo "POSTGRES_PASSWORD vacio en .env" && exit 1)
	@grep -q '^ALEXIS_API_TOKEN=..*' .env || (echo "ALEXIS_API_TOKEN vacio en .env" && exit 1)
	@echo "env-check: OK"

precommit: secrets-check test

# Gate completo antes de commitear o publicar.
check: secrets-check env-check test
