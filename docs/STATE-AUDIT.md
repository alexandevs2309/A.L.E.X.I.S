# STATE AUDIT — estado real por subsistema de ALEXIS

Fecha: 2026-09-26
Commit base: `0ed81fa` (P0 §5.1-§5.5) + 15 archivos sin commitear (brecha #2 token budget, UI/avatar)
Método: recuento de código y de cobertura de tests. **No es una medida de calidad**: un módulo
puede tener muchos tests y seguir siendo incorrecto. Sirve para distinguir *construido* de
*placeholder*, no para certificar *correcto*.

Métricas de contexto: 94 módulos en `alexis/`, 10.874 líneas, 573 tests en 38 ficheros.

**Cómo reproducir los recuentos de líneas** (importante: el método cambia el resultado ~14%):

```bash
# líneas de código, SIN blancos ni comentarios — es el usado en todo el documento
find alexis/<familia> -name '*.py' -exec cat {} + | grep -vcE '^\s*$|^\s*#'
# líneas totales, con blancos y comentarios — NO es el usado aquí
find alexis/<familia> -name '*.py' -exec cat {} + | wc -l
```

Ejemplo: `cognition/` da **3.342** con el primer comando y **3.879** con el segundo.
Mezclar los dos métodos invalida cualquier comparación.

---

## 1. Advertencia de método

Las cifras en % son **estimaciones**, no mediciones. Para lo que sí existe y tiene peso
(los 13 primeras filas) la estimación se apoya en líneas de código y en cuántos ficheros de
test importan el módulo. Para lo que **no existe** el porcentaje es 0 y no hay debate posible.

El error más común al medir así es contar *envoltorios vacíos* como avance. Ver §3.

---

## 2. Estado por subsistema

| Subsistema | Estimación | Evidencia medida |
|---|---|---|
| Arquitectura Core | **~70%** | 94 módulos organizados en capas, pero **3 ciclos de dependencia activos** (§4) y 8 paquetes placeholder (§3) |
| Autonomía / Mission Runtime | ~75% | 459 líneas, importado en 25 ficheros de test. Recovery verificado sólo por el camino legacy (`tests/test_s3_s4.py:249`) |
| Policy / Security | ~80% | 373 líneas, 19 ficheros de test. Gate sin bypass read-only y `requires_approval` vinculante |
| Cognitive Core (P0) | **~35-40%** | Ver `docs/P0-COGNITIVE-CORE-GAP-ANALYSIS.md`. De 22 requisitos: 8 COMPLETE, 11 PARTIAL, 3 MISSING, 0 BROKEN |
| Self Model | ~75% | 553 líneas (`alexis/self/`), 19 ficheros de test. Sin zona de supuestos |
| Memory | ~55-60% | 216 líneas, 12 ficheros de test. `recall()` antes de `decide()` |
| World Model | ~50-60% | 207 líneas, 9 ficheros de test. **Sólo en memoria**: dominios FILE y TOOL, se pierde al reiniciar |
| Model Router | ~65% | 811 líneas, 10 ficheros de test. REAL/DEGRADED/UNAVAILABLE verificado en E2E |
| Replanning | **~100%** | Cerrado: guarda de reintento idéntico con firma determinista, evidencia material con *evidence scope* y auditoría persistente del rechazo. Ver §13 del gap analysis |
| Verification | **~70%** | `goal_verification.py` (596 líneas), 17 ficheros de test, 3 suites nuevas en `0ed81fa` |
| Voice / TTS | ~45% | 427 líneas, 3 backends (`edge`, `elevenlabs`, `local`), 4 ficheros de test |
| Speech input | ~35-40% | **Sólo en `apps/face`** (`SpeechRecognizer.ts`). No existe en el core Python |
| Visual Presence runtime | ~65-70% | 2.357 líneas TypeScript, correctamente cableado a `POST /chat` |
| Avatar final | ~10% | Sin modelo visual ni render 3D |
| **Vision** | **0%** | **0 ficheros. 0 menciones en código** |
| **Browser / Web research** | **0%** | **0 ficheros. 0 módulos** |
| **Git / GitHub intelligence** | **0%** | **0 ficheros. 0 menciones** |
| External integrations | ~10% | Sólo ElevenLabs y Edge TTS (ambos TTS) |
| IoT / environment | 0-5% | 0 ficheros |
| Long-term learning | **~3%** | `alexis/learning/` = **15 líneas**. `ExperienceLearner` sólo hace append: no hay `outcome → reflection → lesson → experience` |
| Paquetes placeholder | **0% funcional** | 8 familias con 112 líneas en total (§3) |

---

## 3. El problema que infla la percepción de avance

Ocho familias son envoltorios vacíos. Sumadas dan **112 líneas**, el 1% del repositorio:

| Familia | Líneas | Fase del plan |
|---|---|---|
| `prediction/` | 3 | — |
| `communication/` | 7 | **P5 — COMMUNICATION** |
| `agents/` | 12 | — |
| `meta/` | 14 | — |
| `experiments/` | 15 | — |
| `learning/` | 15 | **P6 — LEARNING** |
| `observability/` | 18 | — |
| `events/` | 28 | — |

`communication` es P5 del plan y tiene 7 líneas. `learning` es P6 y tiene 15. No son progreso:
son marcadores de posición. Mientras existan, cualquier métrica que sume módulos o directorios
reportará una arquitectura más ancha de lo que es.

---

## 4. Ciclos de dependencia activos

24 de los 94 módulos no se pueden ordenar por capa porque se necesitan mutuamente:

| Ciclo | Módulos | Líneas | Nota |
|---|---|---|---|
| `contracts` ↔ `autonomy.goal_state` | la base del sistema ↔ concepto de P1 | 266 | `contracts.py:88` importa `goal_state`. Introducido en `0ed81fa`. **Flecha invertida** |
| `speech.tts` ↔ `edge`/`elevenlabs`/`local` | base ↔ sus 3 backends | 427 | la fábrica y la base se importan mutuamente |
| `storage` ↔ `storage.db` | paquete ↔ su submódulo | 40 | re-exports dentro del ciclo |

Consecuencia medida: `0ed81fa` tuvo que tocar **33 archivos y 5.090 líneas** para cerrar un
requisito (§5.3-§5.5), porque al meter `goal_state` dentro de `contracts` la definición de tipos
se arrastró a `storage/serialization`, `core/runtime` y los repositorios. El plan pide
incrementos por orden de impacto (línea 48 de `plan_ejecution.md`); esto fue un efecto dominó.

**Regla que lo corrige:** un módulo sólo se escribe cuando todo lo que importa ya existe y es
estable, y `contracts` no importa nada del dominio.

---

## 5. Reconciliación: por qué circulaban "55-60%" y "35-40%" para lo mismo

Antes de este audit, el Core se estimaba en **55-60%**. Este documento lo baja a **35-40%**
(§2). No es una contradicción interna: son dos preguntas distintas, y conviene no mezclarlas.

- **"Cognitive Core está construido" (~55-60%)** = el subsistema existe y recorre el ciclo entero.
  Es la pregunta por la que empezaba la cifra.
- **"P0 está completo" (~35-40%)** = de los 22 requisitos obligatorios del plan, 3 no existen
  (`MISSING`) y 11 están a medias (`PARTIAL`). Es la pregunta que el plan define.

La forma está; el cierre no. Un sistema puede recorrer el ciclo entero y aun así no cumplir
el plan, porque el ciclo no es el requisito: el requisito es cerrar el objetivo con prueba.

**Matiz que ninguna de las dos cifras captura:** en la prueba E2E controlada del 2026-09-25,
el ciclo se ejecutó 4 iteraciones y la observación `NOT_FOUND` **sí** cambió el comportamiento
posterior (disparó replan y cambió la capability), pero **0 de 4 llamadas al modelo devolvieron
respuesta**: todas agotaron el timeout HTTP a 300 s. Todas las decisiones fueron del fallback
determinista. ALEXIS ejecuta el ciclo completo; lo que aún no se ha demostrado es que lo
entienda.

---

## 6. Lo que este documento NO es

- No certifica corrección. 573 tests verdes prueban que el código hace lo que los tests
  esperan, no que lo esperado sea lo correcto.
- No mide calidad de código, ni deuda, ni rendimiento.
- No cubre fases P1-P9 más allá de contar si existe o no código.
- Las cifras de las filas "~75%" son estimaciones honestas, no mediciones reproducibles.
  Lo que sí es reproducible está en §2 con su comando de origen.
