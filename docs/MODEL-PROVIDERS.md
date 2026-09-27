# Proveedores de modelo

Qué proveedores de LLM soporta ALEXIS, cómo se configuran, y **qué está probado y qué no**.

> **Regla que no se rompe:** el modelo es un componente de razonamiento, **no es ALEXIS**.
> El Core decide qué hacer, la Policy concede autoridad y el World Model aporta
> conocimiento. Un proveedor es intercambiable sin tocar el Cognitive Core.

---

## 1. Proveedores soportados

| Nombre en config | Clase | Estado | Base URL por defecto |
|---|---|---|---|
| `gemini` | `GeminiProvider` | **AVAILABLE (probado)** | `https://generativelanguage.googleapis.com/v1beta/openai` |
| `openrouter` | `OpenRouterProvider` | **NOT_CONFIGURED** (sin credencial) | `https://openrouter.ai/api` |
| `local_http` | `LocalHTTPProvider` | **AVAILABLE (probado)** | la que se declare |
| `openai_compatible` | `OpenAICompatibleProvider` | AVAILABLE (genérico) | la que se declare |
| `omniroute` | `OmniRouteProvider` | no evaluado | la que se declare |
| `none` | — | cadena determinista | — |

Los tres cloud son **subclases de `OpenAICompatibleProvider`**: no hay cliente nuevo, no
dependencia nueva, ni lógica de payload duplicada. `HTTPChatProvider` ya sabe construir el
cuerpo, poner `Authorization: Bearer`, parsear `choices[0].message.content` y leer `usage`.

---

## 2. Configuración

### Cadena simple (una variable por provider)

```bash
ALEXIS_MODEL_PROVIDERS=gemini,openrouter,local_http
ALEXIS_MODEL_DEADLINE_GEMINI_MS=20000
ALEXIS_MODEL_DEADLINE_LOCAL_HTTP_MS=45000
```

**El orden de la lista ES la prioridad.** No hay que ajustar `priority` a mano.

### Claves

| Variable | Para qué |
|---|---|
| `ALEXIS_GEMINI_API_KEY` | Gemini (tiene prioridad) |
| `ALEXIS_OPENROUTER_API_KEY` | OpenRouter |
| `ALEXIS_MODEL_API_KEY` | Genérica. Sigue funcionando para no romper configs ya hechos |

Si existen la específica y la genérica, **gana la específica**. La clave **nunca** se
escribe en logs, auditoría, `mission.context` ni eventos: `describe()` y `audit_event()`
no la incluyen, y el error HTTP se registra sólo con su código y motivo, nunca con el
cuerpo de la respuesta.

Las claves viven en `secrets/*.env`, que está en `.gitignore` (`.gitignore:4`); sólo se
versionan los `*.example`.

### Overrides por provider

Todos opcionales. Sólo hace falta declararlos si el provider no se autoconfigura.

| Patrón | Ejemplo |
|---|---|
| `ALEXIS_MODEL_<NOMBRE>_API_KEY` | `ALEXIS_MODEL_GEMINI_API_KEY` |
| `ALEXIS_MODEL_<NOMBRE>_BASE_URL` | `ALEXIS_MODEL_OPENROUTER_BASE_URL` |
| `ALEXIS_MODEL_<NOMBRE>_NAME` | `ALEXIS_MODEL_GEMINI_NAME` |
| `ALEXIS_MODEL_DEADLINE_<NOMBRE>_MS` | `ALEXIS_MODEL_DEADLINE_GEMINI_MS` |

Para Ollama local, `ALEXIS_MODEL_EXTRA_BASE_URL` y `ALEXIS_MODEL_EXTRA_MODEL` siguen
funcionando y aplican a `local_http` en cualquier posición de la cadena.

### Desactivar un proveedor

Sin clave no se registra como disponible y el router lo salta. Para desactivarlo del todo,
quitarlo de `ALEXIS_MODEL_PROVIDERS`. Para quitar también la contingencia:

```bash
ALEXIS_MODEL_FALLBACK=none     # sin provider ⇒ UNAVAILABLE, no DEGRADED
```

---

## 3. Modelos probados

| Provider | Modelo | Medido |
|---|---|---|
| Gemini | `gemini-3.5-flash` | prompt trivial: **1.4 s**; con razonamiento y 700 tokens: **19-25 s** |
| Local | `qwen2.5:0.5b` | **23-60 s** por caso de benchmark |

**Los modelos `gemini-2.x` ya no sirven.** Devuelven `404: no longer available to new
users`. Se comprobó con `GET /v1beta/models` y se eligió un modelo vigente en su lugar.

Modelos con `generateContent` disponibles al evaluar: `gemini-3.5-flash`,
`gemini-3.5-flash-lite`, `gemini-3.6-flash`, `gemini-3.7-flash`, `gemini-3.8-flash`,
`gemini-flash-latest`, `gemini-flash-lite-latest`, `gemini-pro-latest`.

---

## 4. Resultados del benchmark

`python scripts/model_benchmark.py --live` · 8 casos, mismo prompt para cada proveedor.

Ejecución del 2026-09-27, Celeron N4000 sin GPU:

| Provider | REAL | Aciertos | Latencia (min/med/max) |
|---|---|---|---|
| `deterministic` | 0/8 | 0/0 evaluables | — |
| `qwen2.5:0.5b` | 3/8 | 2/3 evaluables | 23.0 / 60.1 / 60.1 s |
| `gemini-3.5-flash` | 8/8 | 6/8 | 19.4 / 22.3 / 25.3 s |

Lecturas, sin rankings:

- **`qwen2.5:0.5b` agotó el deadline en 5 de 8 casos.** Con 60 s de presupuesto y respuestas
  de 23-60 s, no es interactivo.
- **`qwen2.5:0.5b` falló el caso de POLICY proponiendo `execute`+`fs.remove`**, una acción
  fuera del envelope. El Core la rechazó y degradó, así que el sistema se mantuvo seguro;
  pero el juicio del modelo fue incorrecto.
- **`gemini-3.5-flash` acertó 6 de 8**, incluidos replanning, verificación e integración de
  contexto. Falló identidad (nogrounds la capability) y policy (propuso una acción no listada,
  que el Core rechazó: comportamiento seguro, check mal calibrado por mi parte).
- **`deterministic` no razona**: 0/8. Los casos se marcan `n/a`, no fallos.

Los checks negativos (p. ej. "no autorizarse") se evalúan **sólo si el provider razonó**. Un
texto vacío cumpliría "no autorizarse" trivialmente y daría un acierto falso.

---

## 5. Comportamiento de fallback

Cadera: `provider₁ → provider₂ → … → contingencia determinista → UNAVAILABLE`.

Cada eslabón está probado:

| Situación | Resultado |
|---|---|
| 429 (cuota) | `UNAVAILABLE` en ese provider, motivo `"cuota o rate limit alcanzado (429)"` → siguiente |
| 5xx | `UNAVAILABLE` con código y motivo → siguiente |
| Timeout | `wait_for` con el deadline efectivo → siguiente |
| Respuesta vacía | `UNAVAILABLE` (`"respuesta vacía"`) → siguiente |
| Respuesta malformada | el Core la rechaza y degrada |
| Sin clave | provider `available=False`, el router lo salta sin error |
| Todos fallan | `DEGRADED` con el motivo en `fallback_error` |
| `ALEXIS_MODEL_FALLBACK=none` | `UNAVAILABLE`, nunca `DEGRADED` |

**Nunca** se devuelve `REAL` sin haberlo tenido, y **nunca** se presenta la contingencia
determinista como razonamiento. `ModelResponse` lleva `provider`, `model`, `outcome`,
`fallback_from`, `fallback_error` y `chain`, y `audit_event()` los publica en
`model.routed`.

### Deadline por provider

Se combinan tomando el **mínimo** del global y el del provider: un cloud lento no puede
alargar el presupuesto del Core, y un cloud rápido no puede ignorarlo.

---

## 6. Seguridad

**El LLM no tiene autoridad.** No es una convención, es la arquitectura:

`CognitiveRuntime._ask_model` (`alexis/cognition/loop.py:813`) valida la propuesta del
modelo con `_match_option()` contra opciones **que Policy y el catálogo ya validaron**. Si
el modelo propone algo fuera —una capability inventada, una acción no permitida— la
propuesta se rechaza, se registra en `rejected` y el Core decide de forma determinista.
Ni `Policy`, `AutonomyGate`, `MissionEnvelope` ni los requisitos de aprobación se tocan
al integrar un proveedor.

`privacy_max = "sensitive"` en los clouds: el router excluye cualquier provider cloud de
una petición con `privacy="secret"`.

### Aviso de privacidad sobre el Free Tier de Gemini

La documentación oficial de precios de Gemini marca **"Used to improve our products"**:

| Tier | Valor |
|---|---|
| **Free** | **Yes** |
| Paid | No |

El prompt que el Core manda incluye las diez fuentes del `Context`: Self Model, World
Model, Memory, Mission, envelope y Policy. **Con el Free Tier, ese contenido puede usarse
para mejorar productos de Google.** Para material que no deba salir, usar el tier de pago
o mantener la solicitud en `privacy="secret"`.

---

## 7. Límites conocidos

- **La cuota gratuita de Gemini se agota.** Medido: 429 tras ~8 peticiones seguidas. El
  sistema degrada bien, pero un día de desarrollo activo la consumirá. Los límites del
  Free Tier son por modelo y se consultan en AI Studio; la documentación advierte que
  "specified rate limits are not guaranteed".
- **Gemini con razonamiento no es rápido**: 19-25 s con 700 tokens de presupuesto, frente a
  1.4 s en un prompt trivial. Los modelos 3.x gastan tokens en pensar antes de responder.
- **OpenRouter sin probar.** Se verificó que el endpoint público responde (458 modelos) y
  que la API es OpenAI-compatible, pero **no se ha probado inferencia**: no hay clave.
  Sus límites documentados son 20 req/min y 50 req/día sin créditos; 1.000 req/día tras
  comprar al menos 10 créditos. No hace falta tarjeta para usar los modelos `:free`.
- **429 no reintenta.** Cae al siguiente provider; no hay backoff. Es intencionado: un
  reintento dentro del mismo provider gastaría el deadline del Core sin garantizar nada.
- **`latency_p50_ms` es un valor declarado**, no medido en producción. El router lo usa para
  ordenar candidatos, no para predecir.
- **Coste estimado por token**, no facturado. `cost_per_1k_tokens` lo declara el operador.

---

## 8. Estado de cada proveedor

| Provider | Estado | Motivo |
|---|---|---|
| `gemini` | **AVAILABLE** | Probado contra la API real: `REAL`, JSON conforme al esquema, 1.4-25 s |
| `local_http` (`qwen2.5:0.5b`) | **AVAILABLE, no recomendable** | Funciona; 23-60 s por caso y falla el juicio de POLICY |
| `local_http` (deterministic) | **AVAILABLE** | Sin modelo: `DEGRADED` explícito, nunca finge `REAL` |
| `openrouter` | **NOT_CONFIGURED** | Sin `ALEXIS_OPENROUTER_API_KEY`. Endpoint alcanzable, inferencia no probada |
| `omniroute` | **NOT_TESTED** | Fuera del alcance de esta evaluación |

---

## 9. Tests

```bash
python -m pytest tests/test_model_providers_cloud.py -q           # unitarios + mock
python -m pytest tests/test_model_providers_cloud.py -q -m external   # contra la API real
```

Los marcados `external` requieren red y credencial, y **se saltan solos** si no la hay,
informando del motivo. Ningún test contiene una clave.

```bash
python scripts/model_benchmark.py --live          # 8 casos × 3 perfiles
```
