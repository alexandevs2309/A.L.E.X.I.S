# ALEXIS — Interfaz de Experiencia (v0.4, iteración 0)

Cambio dirigido por el humano el 2026-09-21. Reemplaza la interfaz "orb/dashboard" como
experiencia principal.

## 1. Principios

1. **ALEXIS se manifiesta por interacción y contexto.** El orb queda descartado como
   elemento principal. La identidad es un **avatar acompañante holográfico animado**
   (inspirado en *Gideon*, la IA de The Flash/Legends of Tomorrow de CW: azul hielo,
   materiales vítreos, iris luminoso), definido como símbolo SVG inline (sin dependencias
   ni CDN) y reutilizado en cabecera y turnos. Vaga libre por la pantalla (drift), con
   parpadeo, halo pulsante, bobbing y velocidades según contexto; nunca es el centro de
   la interacción, siempre el compañero. La imagen `1000560050.jpg` fue solo referencia
   de diseño, no se usa como objeto de la UI.
2. **Interfaz de inteligencia personal, no dashboard.** Limpia, discreta, centrada en
   la conversación y en las misiones/acciones reales.
3. **Representar el estado real del runtime, y solo ese.** Prohibido presentar como
   "completado" una operación simulada, inventar progreso o mostrar trabajo inexistente
   (BLUEPRINT §10).
4. **Separación de intereses:** Experience (presentación/interacción) ≠ Core (decisión/
   ejecución) ≠ Debug (observabilidad técnica).

## 2. Contextos (Experience Context Model)

Derivados de forma determinista del estado/eventos reales del Core:

| Contexto | Origen real | Qué muestra la UI |
|---|---|---|
| `idle` | Sin misión activa | Pantalla limpia: "¿Qué quieres que haga?" + campo de solicitud |
| `thinking` | `mission.planning` / state PLANNING·VERIFYING·RECOVERING / paso `understand` | Línea de estado "Pensando…" sin tarjetas |
| `researching` | paso `research` en curso | Encabezado + evidencia real (solo observaciones con contenido textual) |
| `executing` | paso `execute` en curso | Avance real: pasos ya completados + paso en curso |
| `waiting_approval` | `mission.approval_required` / state WAITING_APPROVAL | **Bloque de decisión claro**: qué acción, riesgo, por qué, objetivo; botones Aprobar / Cancelar |
| `completed` | state COMPLETED | Resultado + pasos ejecutados + evidencia real + confianza real + siguiente acción |
| `error` | state FAILED / BLOCKED | Qué pasó (sin jerga técnica) y qué necesita ALEXIS del humano |
| `cancelled` | state STOPPED | "Misión cancelada" |

## 3. Límites (qué pertenece a cada capa)

- **Core (`alexis/core/runtime.py`, `alexis/events/bus.py`, planner/policy/executor/**
  verifier/memory): **NO se toca** para resolver el problema visual. Es la única fuente
  de estado y eventos (`mission.*`, pasos `understand/research/execute/verify`).
- **Experience (`alexis/experience/presenter.py` + UI):** traduce el estado real a un
  modelo de presentación humano (contextos, encabezados, decisión, resultado). Es una
  función pura: consume `{mission, verification}` y no produce trabajo.
- **Debug/System (Modo Sistema):** un `details` aparte muestra `/state` crudo (JSON),
  inventario (agentes/herramientas/mundo/memoria) y el log de eventos. Nunca en la
  experiencia principal.

## 4. Honestidad de la presentación

- `steps_done` se construye únicamente desde `mission.results` (eventos reales).
- Encabezados de pasos provienen del vocabulario del presentador (traducción de ids
  reales del planner), con fallback neutro "Trabajando…" si el id es desconocido.
- La evidencia solo se muestra si existe (`verification.evidence`, observaciones con
  texto). Si no hay evidencia real, no se muestra nada (no se rellena).
- La confianza mostrada es la del verifier del runtime. El estado `COMPLETED` se
  presenta tal cual lo reporta el runtime, sin añadir afirmaciones de éxito fabricadas.
- El `LocalExecutor` sigue siendo un placeholder simulado; la UI no lo presenta como
  ejecución real de herramientas (contexto `executing` solo indica el paso real emitido
  por el runtime).

## 5. Arquitectura

```
Core (runtime, events, planner, policy, executor, verifier)
        │  emite: mission.*, step_*, approval_required
        ▼
apps/demo/server.py  ── /state (present) + /missions + /approve + /deny + /stream
        │  state snapshot (real)
        ▼
alexis/experience/presenter.py  ── present(snapshot) → {contexto, encabezados, decisión, resultado}
        ▼
apps/ui.py  ── hilo conversacional + bloqueo de decisión + resultado + avatar acompañante + Modo Sistema (debug)
```

Cambios solo en la capa Experience / demo. El runtime no cambia.