# API — superficie del User Model

> Este fichero documenta **sólo la superficie `/user/*`**, que es lo que añade el User
> Model (`alexis/user/`). La superficie oficial completa (`/health`, `/ui`, `/chat`,
> `/missions*`, `/stream`, `/state`, `/self`, `/capabilities`) está descrita en el
> docstring de `apps/demo/app.py`.

Servidor: la aplicación oficial, `apps/demo/app.py`, sobre `ThreadingHTTPServer`.
Respuestas siempre JSON. Sin PostgreSQL los endpoints `/user/*` responden **503**
(`{"error": "user model no disponible"}`) en vez de 500: la ausencia del User Model es un
estado de diseño —el resto de la API sigue sirviendo— y no un fallo del servidor.

## Modelo de datos

Una sola tabla, `user_preferences`:

| Columna | Tipo | Notas |
|---|---|---|
| `id` | TEXT PK | determinista: `"category:key"` |
| `category` | TEXT | CHECK: `project` \| `style` \| `approval` \| `context` |
| `key` | TEXT | único junto a `category` |
| `value` | JSONB | el contenido; en `project`, lleva `{"active": true\|false}` |
| `confidence` | FLOAT | `NOT NULL DEFAULT 1.0`, CHECK `[0, 1]` |
| `source` | TEXT | CHECK: `explicit` \| `inferred` \| `default` \| `NULL` |

---

## Preferencias

### `GET /user/preferences`

Todas las preferencias, ordenadas por categoría y clave.

```json
[{"id": "style:tono", "category": "style", "key": "tono",
  "value": {"formal": false}, "confidence": 0.9, "source": "explicit"}]
```

### `GET /user/preferences/{category}`

Sólo esa categoría. Categoría inexistente → `[]` con `200`, no `404`: la lista vacía es
una respuesta correcta y un 404 obligaría al cliente a distinguir "no hay nada" de "la
ruta está mal".

### `POST /user/preferences`

Crea o actualiza. El `upsert` conserva `created_at`: reescribir no convierte la
preferencia en nueva, y perder la antigüedad haría imposible distinguir si algo se
corrige o se acaba de descubrir.

```json
{"category": "style", "key": "tono",
 "value": {"formal": false}, "confidence": 0.9, "source": "explicit"}
```

- `category` y `key` son obligatorios → `400`.
- Categoría fuera del CHECK, o `confidence` fuera de `[0, 1]` → `400` con el error de la
  base. **No se recortan en silencio**: un `400` dice que el dato estaba mal, y guardarlo
  "arreglado" escondería que el cliente mandó algo que no cree.
- Respuesta `200` con la preferencia almacenada.

---

## Proyectos

### `GET /user/projects`

```json
{"active_projects": ["ALEXIS"]}
```

Sólo los proyectos con `value.active === true`. Un proyecto **sin** marca `active` está
en estado desconocido, no activo: ofrecer trabajo sobre un proyecto que el usuario cerró
es peor que no ofrecer nada.

### `POST /user/projects`

```json
{"name": "ALEXIS", "details": {"path": "/srv/alexis"}}
```

Marca el proyecto activo y devuelve la preferencia. `400` si falta `name`.

### `DELETE /user/projects/{name}`

Marca el proyecto **inactivo**; no borra la fila. Así conserva su historia y volver a
añadirlo no lo recrea vacío.

- `200` → `{"removed": "ALEXIS"}`
- `404` → el proyecto no existía. `remove_project` es idempotente, pero responder "borré
  algo" cuando no había nada mentiría sobre el efecto.

---

## Decisiones

### `GET /user/decisions`

Decisiones recientes (por defecto las 10 últimas), de la categoría `context`.

```json
[{"decision_id": "d1", "context": {"tarea": "migrar"},
  "outcome": {"resultado": "ok"}, "at": 1757577600}]
```

**No hay endpoint de escritura**: `record_decision` es interno de COGNICIÓN. El contrato
de la superficie es exactamente ese —se escribe por dentro y se lee por fuera—, y por eso
el test escribe por el repositorio y comprueba que la API lo ve. Si algún día se expone,
debe hacerlo con el mismo cuidado que `/user/preferences`.

---

## Integración con el resto del sistema

- **SelfModel** — la zona `user` (`user_preferences`, `active_projects`,
  `recent_decisions`) se publica en `GET /self`.
- **COGNICIÓN** — antes de decidir, `CognitiveRuntime._inyectar_preferencias()` vuelca
  proyectos y estilo en `mission.context['user_preferences']`. No hace nada si no hay
  repositorio o no hay preferencias, y traga cualquier fallo: personalizar nunca puede
  impedir decidir.