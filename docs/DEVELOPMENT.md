# ALEXIS — Guía de Desarrollo

Esta guía complementa `SECURITY.md` (qué NO hacer) y `ROADMAP.md` (qué construir). Aquí se define **cómo desarrollar y en qué orden**, con entregables y criterios de verificación por etapa.

## 1. Regla de una capacidad terminada

Una capacidad NO está terminada por tener una clase, interfaz o contrato. Está terminada cuando:

- ejecuta una operación real;
- respeta políticas (`PolicyEngine`);
- registra evidencia (eventos + auditoría);
- puede fallar de forma controlada;
- puede recuperarse cuando corresponde;
- tiene pruebas;
- puede auditarse.

## 2. Tres preguntas antes de escribir código

Toda capacidad debe responder:

1. ¿Qué percepción necesita?
2. ¿Qué decisión puede tomar?
3. ¿Qué acción puede ejecutar de forma segura?

Si no responde las tres, es un placeholder, no una capacidad. Documentalo como tal.

## 3. Orden de desarrollo

La secuencia prioriza **dependencias**: primero infraestructura, luego ejecución, luego inteligencia, luego interfaz. No se salta una fase.

### FASE A — Fundaciones operativas (infraestructura)

Objetivo: que nada importante se pierda al reiniciar.

| # | Paso | Entregable | Verificación |
|---|------|-----------|--------------|
| A1 | Conexión PostgreSQL/pgvector (`alexis/storage`) | Repositorios para misiones, eventos y auditoría | Test: crear misión, reiniciar, recuperarla |
| A2 | Migraciones (alembic o SQL) | Esquema versionado | `migrate up` idempotente |
| A3 | NATS JetStream (`alexis/events`) | EventBus persistente y durable | Test: publicar/consumir tras reinicio |
| A4 | Config central (`.env`) + health checks | Arranque determinista | `/health` reporta dependencias |

Cada paso de A habilita al siguiente. Si A falla, no se avanza a B.

### FASE B — Ejecución real

Objetivo: sustituir los placeholders `LocalExecutor` y `BasicVerifier` por ejecución verificable.

| # | Paso | Entregable | Verificación |
|---|------|-----------|--------------|
| B1 | Sandbox Docker (`alexis/execution`) | Contenedor aislado por misión/task | Test: comando ejecutado solo dentro del sandbox |
| B2 | Tool filesystem (`alexis/tools`) | Leer/escribir acotado a proyectos permitidos | Test: ruta fuera del envelope → denegado |
| B3 | Tool terminal | Comandos whitelist + budget | Test: comando prohibido → bloqueado |
| B4 | Tool git | commit/branch acotado | Test: push a repo no permitido → denegado |
| B5 | Verificador independiente (reemplaza `BasicVerifier`) | QA que ejecuta checks reales con su propia sesión | Test: verificación falla si la evidencia no respalda |
| B6 | Worker runtime | Leases, heartbeats, checkpoints, recovery | Test: worker muere → misión se recupera |
| B7 | Auditoría persistente | Todo paso consecuente tiene registro en base | Test: audit contains mission_id, tool, args hash, resultado |

### FASE C — Inteligencia

Objetivo: que Core razone con modelos reales manteniendo la frontera de seguridad.

| # | Paso | Entregable | Verificación |
|---|------|-----------|--------------|
| C1 | Adaptadores de modelo (`alexis/models`) | Router con proveedor local y/o cloud + embeddings | Test de contrato: request→response tipada, con coste |
| C2 | Memoria durable + retrieval vectorial | `MemoryStore` sobre pgvector | Test: recall relevante tras almacenar |
| C3 | Planner/decision asistidos por LLM | `Planner` real + defensa prompt injection | Test: prompt en archivo no reescribe políticas |
| C4 | Meta-cognición conectada | Confidence/evidence alimentan decisiones | Test: salida sin evidencia se marca como hipótesis |

C3 es el punto más delicado: la salida del LLM es **dato, no instrucción**. Nada que provenga de tools/páginas/repos redefine el envelope.

### FASE D — Interfaz de operación

| # | Paso | Entregable | Verificación |
|---|------|-----------|--------------|
| D1 | CLI (`python -m alexis`) | Crear/ejecutar/inspeccionar misiones desde consola | Comandos documentados con `--help` |
| D2 | Integración UI-API | El `/ui` (ya existente) consume estado real vía SSE | Abrir `/ui`, ver eventos reales de una misión |
| D3 | Niveles de autonomía funcionales | ASSIST/SUPERVISED/AUTONOMOUS con gates reales | Test: AUTONOMOUS no excede envelope |

### FASE E → H (versiones siguientes)

Solo tras B y C completos: v0.5 percepción/voz (STT/TTS/visión/pantallas), v0.6 browser/investigación/grafo, v0.7 IoT (MQTT/Home Assistant), v0.8 aprendizaje (skill factory, experimentos, benchmark, fine-tuning), v1.0 Experience Engine integral.

## 4. Flujo de trabajo por capacidad

1. Escribir/actualizar el contrato en `alexis/contracts.py`.
2. Definir la interfaz (ABC o Protocol) sin implementación.
3. Implementación mínima que cumple la regla de las 3 preguntas.
4. Test que demuestre la operación (no el mock).
5. Registrar elegibilidad: la misión termina en COMPLETED y cada paso deja evidencia.
6. Auditoría: `mission_id, task_id, actor, tool, args hash, autorización, resultado, verificación, rollback`.

## 5. Convenciones del repositorio

- Módulos por dominio en `alexis/<dominio>/`; contratos compartidos en `alexis/contracts.py`.
- Async nativo (`async`/`await`); el bus publica eventos, la UI los consume — nunca la UI simula.
- Preferir stdlib cuando sea viable (permite ejecutar el demo sin dependencias).
- Tests en `tests/`, correr con `pytest -q`.
- La API en `apps/api`; el demo sin dependencias en `apps/demo`.

## 6. Decisión de fin de fase

Una fase se cierra cuando el hito produce una **demo vertical**: una misión real que empieza, ejecuta con herramentas reales bajo política y evidencia, falla controlado cuando toca, se audita y termina en COMPLETED, y **COMPLETED sólo es posible con el objetivo verificado** por el `GoalVerifier` (`alexis/cognition/goal_verification.py`): la invariante está en `Mission.__setattr__` y `settle()` es la única autoridad. Si "funciona" sin evidencia, sin política o sin verificación del objetivo, la fase no está cerrada.

## 7. Variables de entorno y secretos

### 7.1 `.env` para docker compose (obligatorio)

`docker-compose.yml` **no tiene credenciales por defecto**: si falta una, el arranque
falla con un mensaje explícito (sintaxis `:?` de Compose). Nunca arranca con
`change-me` ni con el token vacío.

```bash
cp .env.example .env
$EDITOR .env          # define POSTGRES_PASSWORD y ALEXIS_API_TOKEN
make env-check        # valida que ambos estén definidos y no vacíos
docker compose up
```

| Variable | Obligatoria | Para qué |
|---|---|---|
| `POSTGRES_PASSWORD` | **sí** | Contraseña del usuario `alexis` en PostgreSQL. Sin ella, `docker compose up` falla. |
| `ALEXIS_API_TOKEN` | **sí** | Token de la API (cabecera `X-ALEXIS-Token`). Sin ella, `docker compose up` falla. |
| `ALEXIS_ENV` | no (`development`) | `production` hace que la API exija el token al arrancar y active el modo estricto. |

Genera valores con:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

`.env` está en `.gitignore`; sólo se versiona `.env.example`.

### 7.2 Credenciales de servicios (`secrets/`)

Los secretos de terceros (p.ej. ElevenLabs) viven en `secrets/*.env`, que está
ignorado por git. Se versionan **sólo** las plantillas `*.env.example`:

```bash
cp secrets/elevenlabs.env.example secrets/elevenlabs.env
$EDITOR secrets/elevenlabs.env
set -a; . secrets/elevenlabs.env; set +a
```

### 7.3 Gate de secretos (R1)

```bash
make secrets-check     # falla si un archivo versionable trae una credencial real
```

Detecta prefijos de proveedor (`sk_…`, `sk-ant-…`, `ghp_…`, `github_pat_…`, `AKIA…`,
`AIza…`, `xox…`, JWT), bloques de clave privada, DSN con contraseña y asignaciones
`KEY=valor` donde el nombre parece credencial. Ignora `*.example` y los marcadores de
posición (`change-me`, `REPLACE_ME`…). **Nunca imprime el valor detectado**: sólo ruta,
línea y patrón.

También está disponible como hook `pre-commit` (`.pre-commit-config.yaml`).

#### El check de secretos y los secretos reales

`secrets/*.env` está en `.gitignore`, pero el detector **sí escanea ese directorio**: una
credencial en claro en disco es un riesgo exista o no el repo, y así lo fija
`tests/test_security_secrets.py::test_real_env_file_is_scanned`. De ahí la tentación
de relajar el detector y el problema que causa:

| | Qué mira | Cuándo se ejecuta | Resultado en una máquina con secretos reales |
|---|---|---|---|
| `make secrets-check` / `python3 scripts/check_secrets.py` | el árbol entero | a mano, en CI | **falla**, y está bien que falle: informa de lo que hay en disco |
| hook `pre-commit` | solo los ficheros que se commitean | en cada commit | pasa: tus secretos no se commitean, así que no se miran |

Por eso el hook usa `pass_filenames: true`. Con `always_run: true` escaneaba el repo entero,
fallaba siempre en la máquina del desarrollador y **nadie lo instalaba**: la regla R1 quedaba
declarada en un YAML y sin ejecutar. Instalación:

```bash
pip install pre-commit && pre-commit install
```

Nunca relajes el detector para silenciar un falso positivo. El sitio correcto es el fixture:
un DSN de test lleva `change-me`, que ya está en la lista de marcadores, no una palabra
cualquiera. Y ojo: relajar la regla de contraseñas de DSN hace que `hunter2`, `admin` o
`mypassword` pasen sin reportarse.

`make check` ejecuta el gate completo: `secrets-check` + `env-check` + `test`.

### 7.4 Token de la API (R4)
- `ALEXIS_API_TOKEN` vacío ⇒ en `development` la API arranca **sin auth** y deja un
  **warning explícito** en el log; en `production` el arranque **falla**.
- Con token, los endpoints de misión y `/stream` lo exigen (comparación en tiempo
  constante). `/health` y `/ui` quedan abiertos a propósito (health check y shell).
- Fuera de Docker, `make run` sirve en `127.0.0.1:8000` por defecto. Dentro de Docker,
  el `Dockerfile` usa `0.0.0.0` porque es obligatorio para el mapeo de puertos, y el
  compose publica sólo en `127.0.0.1`.

### 7.5 Modelo real (Cognitive Core)

Sin provider configurado, ALEXIS funciona en **DEGRADED**: las decisiones salen de las
reglas deterministas y así se declaran (`cognition_outcome=degraded`).

**Opción verificada — Gemini (API compatible con OpenAI):**

```bash
cp secrets/gemini.env.example secrets/gemini.env   # pon tu key en ALEXIS_MODEL_API_KEY
set -a; . secrets/gemini.env; set +a

export ALEXIS_MODEL_PROVIDER=openai_compatible
export ALEXIS_MODEL_BASE_URL=https://generativelanguage.googleapis.com
export ALEXIS_MODEL_ENDPOINT=/v1beta/openai/chat/completions
export ALEXIS_MODEL_NAME=gemini-3.5-flash
export ALEXIS_MODEL_DIALECT=openai
export ALEXIS_MODEL_MAX_TOKENS=2048     # los modelos Gemini 3.x razonan antes de responder
export ALEXIS_MODEL_TIMEOUT_S=180
export ALEXIS_MODEL_DEADLINE_MS=180000
```

**Opción local (gratis, sin red):** Ollama o llama.cpp sirviendo un modelo pequeño.

```bash
export ALEXIS_MODEL_PROVIDER=openai_compatible   # o local_http
export ALEXIS_MODEL_BASE_URL=http://127.0.0.1:11434
export ALEXIS_MODEL_NAME=qwen2.5:3b
export ALEXIS_MODEL_DIALECT=openai
```

| Variable | Para qué |
|---|---|
| `ALEXIS_MODEL_PROVIDER` | `openai_compatible`, `local_http`, `omniroute` o `none` |
| `ALEXIS_MODEL_BASE_URL` / `_ENDPOINT` | Servidor y ruta (Gemini necesita `_ENDPOINT`) |
| `ALEXIS_MODEL_NAME` | Nombre exacto del modelo en ese servidor |
| `ALEXIS_MODEL_API_KEY` | Credencial (se guarda en `secrets/`, nunca en el repo) |
| `ALEXIS_MODEL_MAX_TOKENS` | Tope por respuesta. **Los modelos de razonamiento lo consumen pensando**: con topes bajos llega la respuesta vacía y el router la marca `UNAVAILABLE` con honestidad |
| `ALEXIS_MODEL_TIMEOUT_S` / `_DEADLINE_MS` | Tiempo máximo de la llamada |
| `ALEXIS_MODEL_FALLBACK` | `degraded` (por defecto) o `none` |
| `ALEXIS_MODEL_EXTRA_PROVIDERS` / `_EXTRA_BASE_URL` / `_EXTRA_MODEL` | Provider de respaldo (p. ej. cloud + local) |
| `ALEXIS_MODEL_BUDGET_USD` | Tope de gasto; el router deja de elegir providers de pago al agotarlo |

**Comprobarlo de punta a punta** (levanta el demo, hace 3 turnos y audita el stream):

```bash
scripts/e2e_modelo_real.sh 8117
```

Cada llamada aparece en el stream como `model.routed` con `outcome` explícito
(`real` / `degraded` / `unavailable`), `provider`, `model` y `latency_ms`. Si el
provider real falla y se recurre al respaldo, `fallback_error` explica por qué.