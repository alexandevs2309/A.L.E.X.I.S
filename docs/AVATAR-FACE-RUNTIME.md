# ALEXIS — Avatar/Face Runtime v0.1 (Digital Presence)

> Estado real después de implementar FASE 3–15 de la directiva (`docs/AVATAR-FACE-AUDIT.md`).
> Fecha: 2026-09-21.

## Qué es

Un **Avatar/Face Runtime** de navegador (TypeScript + Three.js + WebGL + GLSL),
desacoplado del Core. El Core solo emite estado; un adaptador lo convierte en
comportamiento visual del rostro. La pantalla principal NO es un dashboard:
solo presencia + interacción mínima; el diagnóstico vive en `?debug=1`.

## Lo que es REAL (verificado)

- Cabeza 3D procedural (no esfera, no 2D) con **18 morph targets reales**
  (`neutral…cheek_compress`) calculados sobre la geometría.
- Face rig con jerarquía `FaceRoot > Head/Neck/Jaw/Eye.L/Eye.R`.
- **GazeController** (presets user/thinking/away/…, damp suave, límites).
- **BlinkController** (no `setInterval` fijo; `random(2.5–7s)`; full/slow/partial/double).
- **HeadMotionController** (yaw/pitch/roll por estado, lentos, pequeños).
- **MicroMotionController** (ruido procedural de baja amplitud; el rostro nunca se congela).
- **FaceBehaviorEngine** con los 10 estados: `idle/listening/thinking/researching/
  focused/speaking/waiting/success/warning/error`, cada uno con parámetros visuales.
- **NeuralMaterial** (base oscura + emission cian/azul; puntos GLSL; líneas de malla;
  intensidad por estado) y **ParticleSystem** (Points GPU, actividad por estado,
  patrón `error` distinto).
- **API** `setAlexisState(state, ...)` y **`CoreStateAdapter`** que consume `/state`
  y `/stream` del demo y mapea el estado real (MissionState + presenter) → estado visual.
- DEBUG overlay solo con `?debug=1` (FPS, estado, gaze, morphs, tris/drawcalls).
- Integración demo: `/` → Face App; `/classic` → UI conversacional anterior;
  `/avatar`, `/face/*` → assets.

## Lo que es HONESTAMENTE pendiente (NO fingido)

- **No existe modelo GLB/glTF.** El rostro actual es un **placeholder técnicamente
  correcto** (geometría procedural con morphs reales) pensado para que el pipeline
  no cambie cuando llegue el asset.
- **Voz SÍ existe desde FASE 16** (Web Speech API del navegador, sin servidor TTS):
  `VoiceDriver` habla en **español latino** (prioriza es-419: es-MX/es-US/es-AR… sobre
  es-ES), narra el reporte de cada misión, pide decisión al esperar autorización y
  saluda al abrir. La boca se mueve **a tiempo** mientras suena (aproximación, no
  fonema).
- **Lip-sync por fonema NO existe (RESERVADO):** `LipSyncEngine`/`VisemeController`
  siguen siendo la interfaz preparada; `speaking` ahora refleja la voz real, pero el
  análisis de fonemas → visemas exactos (mouth shape por cada sonido) queda pendiente.
- **Jaw como bone** existe en la jerarquía, pero el desplazamiento visual se hace
  por morph target (sin skinning hasta el GLB).
- **Verificación WebGL en navegador real**: pendiente de revisión humana visual
  (el contenedor no ejecuta GPU). Build, servido estático y flujo Core→estado sí
  verificados.

## Cómo ejecutarlo

```bash
cd apps/face
npm install          # three + vite + typescript
npm run build        # tsc --noEmit + vite build → dist/
# el demo (python:3.12-slim) sirve dist en:  /  /avatar  /face/*
python3 -m apps.demo.server    # o el contenedor alexis-demo
```

Ir a `http://127.0.0.1:8100/`. Para diagnóstico: `http://127.0.0.1:8100/?debug=1`.

## Modelo 3D pendiente (para sustituir el placeholder)

Requisitos documentados (audit §7):

- glTF 2.0 / GLB, cabeza humanoide, 5–15k triángulos.
- Morph targets: `neutral, blink_left, blink_right, blink_both, brow_up, brow_down,
  brow_inner, eye_squint, mouth_open, mouth_close, mouth_smile, mouth_frown,
  mouth_round, jaw_open, jaw_left, jaw_right, cheek_raise, cheek_compress`.
- Esqueleto: `Head / Neck / Jaw / Eye.L / Eye.R / FaceRoot`.
- Materiales con canal emissive (cian/azul) + normal y máscara.
- Pipeline de integración listo: `THREE.GLTFLoader` + registro de morphs/skeleton;
  intercambio = colocar `.glb` en `apps/face/assets` y activar el `ModelRegistry`.

## Regla de avance

- No se considera "rostro real terminado" mientras se use el placeholder.
- No se afirma lip-sync por fonema: la boca abre a tiempo mientras suena la Voz real,
  pero el análisis de fonemas → visemas no está implementado.
- No se modifica el Core para resolver problemas visuales.