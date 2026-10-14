PYTHON ?= python3
VENV_PYTHON ?= .venv/bin/python

# CORE-03: el runtime oficial es `apps/demo`. `apps/api` es una fachada del mismo
# runtime, no un segundo ALEXIS: por eso tiene su propio target.
run:
	$(PYTHON) -m apps.demo.server

run-api:
	uvicorn apps.api.main:app --host 127.0.0.1 --port 8000

test:
	pytest -q

# EL PROCEDIMIENTO CORRECTO DE LA SUITE. Va aquí y no en `test` a propósito:
#
# `launch-isolated.sh` levanta un net-ns con CERO rutas. Ahí el DNS no existe, y por eso las
# pruebas marcadas `external` —que necesitan resolver nombres de verdad para distinguir
# «permitido» de «no verificable»— tienen su premisa anulada y fallan. No es un defecto: es
# que son pruebas de otro entorno.
#
# Lo que NO se hace aquí, y por qué: no hay un hook en `conftest.py` que salte `external`
# automáticamente. Un hook así cambiaría el comportamiento de pytest en TODOS los entornos,
# incluido el que sí tiene red, donde esas pruebas deben correr. La exclusión es explícita
# y está en el comando, que es donde se puede ver, auditar y revertir.
#
# Las pruebas de `external` no desaparecen de la suite general: son `make test-external`, y
# son las que `test_isolation_gate.py` sustituye bajo aislamiento. Ver docs/DISENO-AISLAMIENTO.md.
test-isolated:
	scripts/launch-isolated.sh $(VENV_PYTHON) -m pytest -q -m "not external"

# Las 7 pruebas `external`, que exigen red real y credenciales. Fuera del aislamiento, a
# propósito: aquí el DNS existe y es la única forma de que signifiquen algo.
test-external:
	$(VENV_PYTHON) -m pytest -q -m external

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
