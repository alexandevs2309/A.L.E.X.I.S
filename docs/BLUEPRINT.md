# ALEXIS — Blueprint Maestro

## 1. Propósito

ALEXIS es un sistema personal de inteligencia artificial orientado a objetivos, capaz de percibir, razonar, planificar, ejecutar, verificar, recordar y aprender dentro de límites de seguridad explícitos.

No se define como chatbot, asistente de programación ni avatar. Esos son componentes o interfaces de un sistema mayor.

## 2. Principio arquitectónico central

**ALEXIS Core piensa y coordina. Los subsistemas ejecutan.**

El avatar no es ALEXIS.
Un modelo LLM no es ALEXIS.
Un agente especializado no es ALEXIS.
Una memoria no es ALEXIS.

ALEXIS emerge de la coordinación controlada de todos ellos.

## 3. Bucle operativo

`Goal → Context → Plan → Policy → Execute → Observe → Evaluate → Replan → Verify → Commit → Learn`

Este bucle debe soportar tanto una interacción de segundos como una misión de horas.

## 4. Niveles de autonomía

### ASSIST
ALEXIS analiza y propone.

### SUPERVISED
ALEXIS ejecuta tareas permitidas y solicita aprobación cuando corresponde.

### AUTONOMOUS
ALEXIS ejecuta misiones dentro de un Mission Envelope previamente definido.

## 5. Mission Envelope

Cada misión debe declarar:

- objetivo
- duración máxima
- proyectos permitidos
- acciones permitidas
- acciones prohibidas
- acciones que requieren aprobación
- presupuesto de recursos
- criterios de éxito
- condiciones de parada

## 6. Principio de evidencia

ALEXIS debe separar:

- FACT
- EVIDENCE
- INFERENCE
- ASSUMPTION
- UNCERTAINTY

Una salida generada por un modelo no se convierte automáticamente en conocimiento.

## 7. Verificación

Toda acción relevante debe poder ser verificada independientemente de quien la ejecutó.

Ejemplo:

```text
Coder
  ↓
implementación
  ↓
QA
  ↓
tests
  ↓
Critic
  ↓
evaluación
  ↓
ALEXIS Core
```

## 8. Aprendizaje

El aprendizaje sigue:

`Experience → Outcome → Evaluation → Reflection → Lesson → Skill Candidate → Tests → Version`

ALEXIS no debe reescribir arbitrariamente sus propias políticas de seguridad o autoridad.

## 9. Mundo físico

La arquitectura contempla cámaras, micrófonos, sensores, MQTT, Home Assistant y actuadores. Las acciones físicas requieren políticas específicas y mayor nivel de autorización.

## 10. Experience Engine

La experiencia visual puede mostrar:

- estado de ALEXIS
- misión actual
- agentes trabajando
- herramientas
- evidencia
- terminal
- código
- gráficos
- mapas
- alertas
- voz
- avatar 3D

La interfaz principal de ALEXIS NO es un avatar ni un objeto visual permanente: se
manifiesta mediante la interacción y el contexto (conversación, misiones, decisiones y
resultados reales). El resto de los modos (avatar 3D, terminal, gráficos, mapas, voz)
son modos opcionales o de diagnóstico, nunca el elemento principal.

La interfaz debe reflejar actividad real. No debe simular trabajo inexistente.

## 11. Regla de diseño

Cada nueva capacidad debe responder tres preguntas:

1. ¿Qué percepción necesita?
2. ¿Qué decisión puede tomar?
3. ¿Qué acción puede ejecutar de forma segura?

Si no puede responderse a esas tres, todavía no es una capacidad completa.
