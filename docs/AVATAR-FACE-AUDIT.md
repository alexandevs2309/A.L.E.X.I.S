# ALEXIS — AVATAR/FACE RUNTIME · Proyecto Digital Presence v0.1

> Auditoría previa al desarrollo (FASE 1 de la directiva).
> Fecha: 2026-09-21 · Estado: **auditoría hecha, implementación FASE 3–15 en curso
> (ver `docs/AVATAR-FACE-RUNTIME.md`)**.
> Reglas de oro: no falsificar capacidades; un modelo 3D real se declara solo si
> existe; el Core solo emite estados; la experiencia normal NO es un dashboard.

---

## 1. Resumen ejecutivo

ALEXIS no tiene hoy ningún rostro 3D. La interfaz es un HTML servido por un
servidor Python stdlib (`apps/demo/server.py`) con un **compañero 2D SVG animado**
que reemplazó al antiguo orb. No existe `package.json`, ni Three.js, ni Vite, ni
ningún asset GLB/glTF/TS/fbx en el repositorio.

Se va a construir **ALEXIS Digital Presence v0.1**: un **Avatar/Face Runtime**
navegador (TypeScript + Three.js + WebGL + GLSL) desacoplado del Core. El Core
sigue siendo la única fuente de estado; un adaptador convierte sus estados/eventos
en estados visuales del rostro. Como no existe modelo GLB, se construirá un
**placeholder 3D técnicamente correcto** (geometría procedural con morph targets
reales, material neuronal GLSL, partículas, gaze, blink, head motion) y se
documentará el asset 3D faltante y el pipeline para sustituirlo.

---

## 2. Arquitectura actual (lo que hay hoy)

```
Python 3.12 (sin Node en el contenedor demo; NODE v24 y npm 12 SÍ en el host)
alexis/core/runtime.py ── emite: MissionState + eventos mission.*, step_*
        │
apps/demo/server.py (http.server, puerto 8100)
        ├── /state   → estado real {mission, verification, present, agents, tools, world, memory, events_count}
        ├── /stream  → SSE de eventos (EventBus subscribe_async)
        ├── /missions, /missions/{id}/approve|deny
        ├── / (PAGE) → HTML único de apps/ui.py (conversación + Modo Sistema)
        └── /face.jpg (referencia estética 1000560050.jpg)
        │
alexis/experience/presenter.py ── present(snapshot) → contexto humano:
        idle | thinking | researching | executing | waiting_approval
        | completed | cancelled | error
apps/ui.py ── HTML/CSS/JS inline: hilo conversacional, bloque de decisión,
        resultado, Modo Sistema (/state crudo), avatar SVG 2D acompañante.
```

- **Estado del Core** (`alexis/contracts.py`): `pending, planning, running,
  verifying, waiting_approval, blocked, recovering, completed, failed, stopped`.
  Un foco de paso (`understand/research/execute/verify`) está en `results`.
- **Presenter**: traduce estado del Core a contextos hum****anos; es una función
  pura (`present(snapshot)`), sin efectos. **Reutilizable como puente.**
- **EventBus**: `publish(topic, payload)` con suscriptores; el demo ya lo expone
  como SSE. Fuente para reacciones faciales puntuales (events gansted de
  `mission.step_started`, `mission.approval_required`, `mission.completed`…).
- **Nodo desarrollador**: host SIN pip/venv, PERO con Node v24/npm 12.

---

## 3. Componentes reutilizables

| Pieza | Ubicación | Por qué se reutiliza |
|---|---|---|
| `presenter.present()` | `alexis/experience/presenter.py` | Ya traduce Core → contexto; el adaptador del rostro lo consumirá |
| EventBus + `/stream` SSE | `alexis/events/bus.py`, `apps/demo/server.py` | Canal vivo de eventos del Core |
| `/state` JSON | `apps/demo/server.py` | Snapshot con `mission.state`, `results`, `verification.confidence`, `present.context` |
| `MissionState` + steps | `alexis/contracts.py` | Fuente de verdad para el mapeo de estados visuales |
| Referencia estética | `1000560050.jpg` | **Solo guía artística**; nunca textura/detector 2D |
| Servidor demo + endpoints approve/deny | `apps/demo/server.py` | Backend de la experiencia conversacional que convivirá con el rostro |
| `docs/EXPERIENCE-UI.md` | — | Principios de presentación honesta y separación Experience/Core/Debug |

---

## 4. Componentes a eliminar / desacoplar

- **Orb**: ya eliminado de la experiencia principal (0 ocurrencias). Confirmar que
  la nueva página no re-introduce ningún objeto esférico monocromo.
- **Compañero SVG 2D actual** (`#companion` en `apps/ui.py`): es 2D, no cumple la
  directiva. En la experiencia principal será **sustituido** por el rostro 3D. El
  símbolo `gideon` (nombre provisional a revisar: un ayudante 2D independiente)
  puede seguir como **mini-icono de turnos en el chat compacto**, NO como presencia.
- **`apps/ui.py` como página raíz**: una vez maduro el Face App, `/` pasará a ser
  el Face App; la conversación queda como overlay compacto y el Modo Sistema
  solo en toggle `Sistema` (fuera de la experiencia normal). `apps/ui.py` se
  conserva como referencia transicional hasta que el Face App sea funcional.
- **No se toca** `alexis/core/*` ni `alexis/storage/*` para resolver nada visual.

---

## 5. Componentes a crear (estructura propuesta)

Aplicando el layout de la directiva a este repo (frontend TS independiente):

```
apps/face/                        # Vite + TypeScript (nuevo)
  package.json  vite.config.ts  index.html  tsconfig.json
  src/
    main.ts                       # bootstrap: Scene + Face + adaptador
    state/
      AlexisVisualState.ts        # estados visuales + API setAlexisState()
      CoreStateAdapter.ts         # fetch /state + SSE /stream → AlexisVisualState
    avatar/
      AlexisFace.ts               # ensambla rig + controllers
      FaceRig.ts                  # Head / Neck / Jaw / Eye.L / Eye.R / FaceRoot
      MorphController.ts          # blendshapes (neutral… jaw_right)
      BoneController.ts           # skeleton (prep, hueco hasta GLB)
      MaterialController.ts       # registra NeuralMaterial en el rostro
    behavior/
      FaceBehaviorEngine.ts       # máquina de estados visuales (10 estados)
      GazeController.ts           # damp·lerp → eye rotation
      BlinkController.ts          # random(2.5–7s), slow/partial/double
      ExpressionController.ts     # targets por estado
      HeadMotionController.ts     # yaw/pitch/roll mínimos por estado
      MicroMotionController.ts    # ruido procedural de baja amplitud + respirar
    speech/
      LipSyncEngine.ts            # STUB honesto (interfaz, sin TTS)
      VisemeController.ts         # STUB honesto (preparado, sin blendshapes de voz)
    rendering/
      Scene.ts  Camera.ts  NeuralMaterial.ts  ParticleSystem.ts  PostProcessing.ts  Debug.ts
```

No se duplica nada que exista (no hay runtime visual previo reutilizable).

---

## 6. Dependencias necesarias

| Dep | Tipo | Justificación | ¿Necesaria? |
|---|---|---|---|
| `three` | runtime | motor WebGL/WebGPU-ready, escena, materiales, morph targets | **SÍ** (núcleo) |
| `vite` | dev | bundler/serve del Face App; el repo no tiene Vite hoy, se introduce aquí solo | **SÍ** |
| `typescript` | dev | lenguaje de la directiva | **SÍ** |
| `vite-plugin-glsl` | dev | importar shaders .glsl sin hacks de strings | Opcional (si se quiere) |
| `gsap` | runtime | interpolaciones; se puede sustituir por un `damp()` propio (~20 líneas, sin dep) | **NO por defecto**: primero `damp()` propio |

- **Sin CDN**: se sirve el bundle construido desde el propio servidor del demo.
- La build se hace **en el host (Node disponible)** y el contenedor solo sirve
  `dist` (el contenedor `python:3.12-slim` no tiene Node).
- Nada de esto toca el `pyproject.toml` de Python (salvo, opcional, un script
  `make face-build`).

---

## 7. Modelo 3D requerido

**Hoy NO existe ningún GLB/glTF/fbx en el repo. No se afirmará que existe.**

Requisitos del asset (para la fase de integración posterior):

- Formato **glTF 2.0 / GLB**, geometría de **cabeza humanoide** (no esfera);
  ideal 5k–15k triángulos.
- **Morph targets** (al menos el set de la directiva): `neutral, blink_left,
  blink_right, blink_both, brow_up, brow_down, brow_inner, eye_squint,
  mouth_open, mouth_close, mouth_smile, mouth_frown, mouth_round, jaw_open,
  jaw_left, jaw_right, cheek_raise, cheek_compress`.
- **Esqueleto**: `Head / Neck / Jaw / Eye.L / Eye.R / FaceRoot`.
- Materiales con canal **emissive** (cian/azul) + normal y mapa de máscara.
- Animaciones (bonus): idle respirar, rubios (rubios de referencia, no obligatorios).

**Placeholder hasta tenerlo (técnicamente correcto, NO fake):**
- Cabeza construida con **geometría procedural** (SphereGeometry segmentada,
  achatada y sculpeada por desplazamiento programático en BufferGeometry) que
  NO sea una esfera simple ni un objeto final: contará con rig de huesos apto
  (objetos jerárquicos) y **morph targets reales** (offsets de vértices) para
  blink/jaw/brow/mouth, para que todo el pipeline (MorphController,
  ExpressionController, Gaze, Blink) funcione con datos reales y el GLB solo
  se intercambie como asset.
- El material neuronal, partículas, ojos, parpadeo y micro-movimiento se aplican
  igual al placeholder, para que la base sea la definitiva.
- **Pipeline GLB**: `GLTFLoader` + carga de morphs/skeleton ya preparada; el
  intercambio es "poner el .glb en assets y activar `ModelRegistry`".
- Se documenta qué asset falta y sus requisitos (sección de arriba) de forma
  explícita; no se simula un rostro "real" terminado.

---

## 8. Mapeo Core → Estados visuales (adaptador)

`CoreStateAdapter` consume `/state` (y el SSE para latencia) y deriva
`AlexisVisualState`. Prioridad: estado de misión real > contexto del presenter.

| Core (real) | AlexisVisualState |
|---|---|
| sin misión + input en foco / escribiendo | **LISTENING** (attention `user`) |
| sin misión + sin interacción | **IDLE** |
| `pending` / `planning` / `verifying` / paso `understand` | **THINKING** |
| paso `research` | **RESEARCHING** |
| paso `execute` | **FOCUSED** |
| `waiting_approval` | **WAITING** |
| `completed` | **SUCCESS** (glow breve) |
| `recovering` | **WARNING** |
| `failed` / `blocked` | **ERROR** |
| `stopped` | **IDLE** (actividad baja) |
| — (reservado, sin TTS todavía) | **SPEAKING** → `speaking:false` hasta que exista voz |

Payload visual que el runtime consumirá (objetivo de contrato):

```jsonc
{
  "state": "thinking",        // AlexisVisualState
  "activity": "researching",  // sub-actividad detallada
  "attention": "user",        // user | thinking | away
  "speaking": false,          // reservado para TTS
  "confidence": 0.82          // de runtime verify
}
```

Este objeto lo construye el **adaptador** (nuevo, en frontend TS) a partir de
`/state` + presenter; el Core **no cambia** y solo emite sus estados/eventos.

---

## 9. Diseño de los sistemas (resumen técnico)

- **GazeController**: orientación de los ojos hacia target (`user`, `left/right/up/
  down`, `thinking`, `neutral`) con **damp/lerp** exponencial por frame
  (p. ej. `x += (target-x) * (1 - exp(-k·dt))`); nunca saltos; visión clara.
- **BlinkController**: `nextBlink = random(2.5s, 7s)`; tipos: full, slow
  (eyelid más dorsal), partial (cierre ~50%), **double**; stochastics independend
  izquierda/derecha; entre parpadeos *micro-saccades* de ojos.
- **HeadMotionController**: yaw/pitch/roll por estado con matrices acotadas
  (IDLE ±0.8°, THINKING desvío leve ±1.2°, LISTENING orientación al usuario,
  ERROR más contenido); ruta suave con damp.
- **MicroMotionController**: ruido procedural (suma de senos / `Perlin3D` simple en
  CPU para pocos parámetros, o uniform en shader para partículas y desplazamiento
  sutil de malla); amplitud <1 px equivalente; guía anti-"congelado".
- **NeuralMaterial**: `ShaderMaterial` GLSL — base oscura, **emission cian/azul**,
  alpha blending selectivo, **points+lines** sobre la superficie (wireframe fino +
  puntos neuronales), noise animado en `time`, intensidad con uniform por estado.
- **ParticleSystem**: `THREE.Points` con `BufferGeometry` (miles de puntos rinde
  en GPU, sin crear miles de objetos), puntos que siguen aproximadamente la
  superficie del rostro + deriva mínima; uniform de **actividad** por estado
  (IDLE baja, THINKING moderada, RESEARCHING alta, ERROR patrón distinto/denso
  bajo).
- **Eyes**: dos mesas-órbitas independientes con material emissive, iris digital
  (textura procedural programática o shader), glow (sprite/`AdditiveBlending`),
  intensidad por estado.
- **Performance**: un solo renderer; geometrías compartidas/merged donde aplique;
  `Points`/`InstancedMesh` en vez de miles de objetos; `requestAnimationFrame`;
  cámaras y luces mínimas; desactivar shadows si no aportan; GUI fija de resolución.
  Arquitectura listada para **WebGPU** (Three.js ya la soporta) sin migrar.
- **Debug** (`Debug.ts`): overlay **solo** con `?debug=1` (o toggle oculto):
  FPS, morph weights, gaze target, estado actual, renderer info. Nunca visible en
  experiencia normal.

---

## 10. Integración con el demo (routing) y la conversación

1. Build en host: `cd apps/face && npm install && npm run build` → `dist/`.
2. `apps/demo/server.py` añade servido estático de `dist/` bajo `/face/`
   (mimetypes) — cambio acotado de la capa demo, NO del Core.
3. Experiencia normal (`/` → Face App): **rostro 3D como presencia** + interacción
   mínima (composer compacto + mini-chat); el Modo Sistema sigue existiendo pero
   oculto detrás del toggle y con `?debug=1` para el overlay técnico.
4. Transición: mientras el Face App no sea funcional, la página actual
   (`apps/ui.py`) sigue siendo la raíz sin borrar nada.

---

## 11. Riesgos

| Riesgo | Mitigación |
|---|---|
| No hay modelo GLB → no se puede mostrar rostro "real" | Placeholder 3D procedural real con morphs; se documenta el asset faltante y requisitos |
| WebGL no ejecutable en el contenedor de test | Verificación por **build + sirve estático** + revisión visual en navegador (manual). Lógica pura (gaze/blink/estados) testeable con vitest (devDep opcional) |
| Bundle Three.js grande | Aceptable en demo; separar chunks si el árbol crece |
| Host tiene Node, contenedor no | Construir en host; commit de `dist`; `make face-build` documenta el paso |
| Riesgo de "esfera otra vez" | Criterio 25: geometría no esférica, rostro reconocible, con ojos/boca; QA visual explícito anti-orb |
| Falsas capacidades (lip-sync/TTS) | `LipSyncEngine`/`VisemeController` como **interfaces preparadas**, con `speaking:false` y sin afirmaciones de voz |
| Cambiar Core para arreglar lo visual | Prohibido: el Core solo emite estados; todo lo visual vive en `apps/face` + adaptador |

---

## 12. Estrategia de implementación

Fases de la directiva, con este orden concreto:

- **FASE 1 · Auditoría** → este documento. ✅
- **FASE 2 · Eliminar/desacoplar** el orb y el compañero 2D de la experiencia
  principal (mantener mini-icono de chat).
- **FASE 3 · Avatar Runtime**: scaffold `apps/face` (Vite+TS+three) + `main` que
  renderiza escena en un contenedor y arranca Debug en `?debug=1`.
- **FASE 4 · Modelo**: confirmar que no hay GLB (ya confirmado) → **placeholder
  procedural con morphs reales** + documento de requisitos del GLB + `GLTFLoader`
  listo.
- **FASE 5 · FaceRig/MorphController** (Head/Neck/Jaw/Eye.L/Eye.R/FaceRoot;
  blendshapes del §5 de la directiva).
- **FASE 6 · GazeController** · 7 · BlinkController · 8 · HeadMotionController ·
  9 · MicroMotionController.
- **FASE 10 · FaceBehaviorEngine** (10 estados + parámetros por estado).
- **FASE 11 · NeuralMaterial** (GLSL) · 12 · ParticleSystem.
- **FASE 13 · Estados visuales** → `setAlexisState(...)` + API pública.
- **FASE 14 · LipSync/Viseme**: interfaces preparadas (sin TTS).
- **FASE 15 · Integración Core→Rostro**: `CoreStateAdapter` con `/state`+SSE,
  mapa del §8, y pagina raíz del demo → Face App (conversación compacta + Modo
  Sistema oculto).

Criterio de éxito (directiva §25): rostro 3D, mirada a cámara, parpadeos
naturales, micro-movimiento, head motion leve, cambio de estados, expresiones,
material neuronal, partículas, reacción al estado real del Core, frames estables,
sin orb. **Presencia digital, no dashboard, no esfera.**

## 13. No hacer (recordatorio directiva §22)

No recrear orb/esfera; no usar la imagen como solución final; no usar imagen como
textura principal; no animación pregrabada falsa; no fingir lip-sync ni modelo 3D;
no dependencias innecesarias; no reescribir el Core; no modificar el backend para
problemas visuales; no construir toda la plataforma de IA ahora.