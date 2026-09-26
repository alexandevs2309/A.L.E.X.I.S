Quiero que implementes ALEXIS siguiendo estrictamente este plan.

OBJETIVO:
Llevar ALEXIS desde su estado actual hasta una inteligencia personal autónoma funcional. La regla principal es: TERMINAR COMPLETAMENTE UNA FASE ANTES DE PASAR A LA SIGUIENTE.

ORDEN OBLIGATORIO:

P0 — COGNITIVE CORE
P1 — AUTONOMY
P2 — MEMORY + WORLD MODEL
P3 — CAPABILITIES
P4 — PERCEPTION
P5 — COMMUNICATION
P6 — LEARNING
P7 — PRESENCE
P8 — ENVIRONMENT
P9 — ALEXIS 1.0

REGLA PRINCIPAL:
NO avances a P1 mientras P0 no esté realmente al 100%.
NO avances a una fase posterior por interés, conveniencia o porque alguna funcionalidad sea más fácil.
Primero profundidad, después amplitud.

IMPORTANTE:
El repositorio actual es la fuente de verdad. Antes de modificar código, inspecciona lo que ya existe. Conserva y extiende las implementaciones funcionales. NO hagas una reescritura innecesaria ni dupliques sistemas existentes.

==================================================
P0 — COGNITIVE CORE
==================================================

Esta es la única fase que debes trabajar AHORA.

Primero realiza un AUDIT completo del estado actual de P0.

Clasifica cada requisito como:

COMPLETE
PARTIAL
MISSING
BROKEN

Crea:

docs/P0-COGNITIVE-CORE-GAP-ANALYSIS.md

NO empieces a programar antes de completar este análisis.

Después de identificar los GAPs, implementa cada uno por orden de impacto.

El Cognitive Core debe terminar siendo capaz de realizar este ciclo:

USER GOAL
→ CONTEXT
→ UNDERSTAND
→ DECIDE
→ SELECT CAPABILITY
→ PLAN
→ POLICY
→ GATE
→ EXECUTE
→ OBSERVE
→ EVALUATE
→ REPLAN si es necesario
→ VERIFY
→ REFLECT
→ RESPOND
→ UPDATE MEMORY / SELF MODEL

P0 debe cubrir obligatoriamente:

1. Intent Engine
   - greeting
   - question
   - conversation
   - capability query
   - task
   - command
   - clarification
   - Solo las tareas reales deben crear missions.

2. Context Assembly
   Integrar:
   - conversación
   - Self Model
   - World Model
   - Memory
   - Mission
   - Mission Envelope
   - Policy
   - capabilities disponibles
   - observaciones anteriores
   - incertidumbres

3. Self Model
   Debe participar realmente en las decisiones.
   ALEXIS debe saber:
   - quién es
   - qué está haciendo
   - cuál es su objetivo
   - qué capacidades tiene
   - qué capacidades no tiene
   - qué está autorizado a hacer
   - qué restricciones tiene
   - qué sabe
   - qué no sabe
   - qué supone
   - qué evidencia tiene
   - qué salió mal
   - qué debe hacer después

4. World Model
   Debe utilizar información observada realmente por las herramientas y no inventar estado del mundo.

5. Memory
   El Cognitive Core debe poder recuperar memoria relevante antes de decidir.

6. Capability Selection
   La selección de herramientas/capacidades debe ser dinámica.
   NO usar una secuencia fija como:
   understand → research → execute → verify.
   El plan debe depender del objetivo.

7. Dynamic Planning
   Los planes deben construirse dinámicamente.
   Cada paso debe tener:
   - objective
   - capability
   - action
   - arguments
   - dependencies
   - expected observation
   - success criteria
   - risk
   - authorization requirement

8. Execution
   Toda acción debe pasar obligatoriamente por:
   Decision → Policy → Gate → Execution.

   El modelo nunca puede concederse permisos a sí mismo.

9. Observation
   Cada ejecución debe producir una observación estructurada y regresar al Cognitive Core.

10. Evaluation
   Después de cada acción determinar:
   - SUCCESS
   - PARTIAL_SUCCESS
   - FAILURE
   - INSUFFICIENT_EVIDENCE
   - BLOCKED

11. Success Criteria
   Nunca confundir:
   "la herramienta ejecutó correctamente"
   con:
   "el objetivo fue conseguido".

12. Replanning
   Si una acción falla:
   analizar → cambiar estrategia → ejecutar alternativa → observar → evaluar.

   Evitar repetir indefinidamente la misma acción con los mismos argumentos.

13. Ask User
   ALEXIS debe detenerse y preguntar cuando:
   - falta información crítica
   - requiere aprobación
   - está fuera del envelope
   - existen interpretaciones ambiguas
   - no posee la capacidad necesaria
   - existe incertidumbre que impide continuar correctamente

14. Evidence / Claim Guard
   Diferenciar siempre:
   FACT
   EVIDENCE
   INFERENCE
   ASSUMPTION
   UNCERTAINTY

   Una afirmación del modelo nunca se convierte automáticamente en FACT.

15. Independent Verification
   El modelo no puede ser el único verificador.
   El resultado debe verificarse mediante evidencia independiente cuando corresponda.

16. Response Composer
   La respuesta final debe basarse en:
   - acciones reales
   - observaciones
   - evidencia
   - verificación
   - incertidumbre
   - estado final

   Nunca decir "listo" si el objetivo no fue realmente verificado.

17. Reflection
   Al finalizar una misión:
   outcome → reflection → lesson → experience.

18. Persistence
   Persistir:
   - mission
   - state
   - context
   - plan
   - current step
   - observations
   - evidence
   - claims
   - decisions
   - replans
   - approvals
   - verification
   - reflection

19. Recovery
   Después de reiniciar ALEXIS debe poder recuperar una misión existente desde su checkpoint y continuar.

20. Model Router
   Mantener separación estricta:
   Cognitive Core decide QUÉ hacer.
   Model Router decide QUÉ modelo/provider utilizar.

   Estados obligatorios:
   REAL
   DEGRADED
   UNAVAILABLE

   Nunca presentar DEGRADED como REAL.

21. Behavioral Tests
   Crear pruebas reales para:
   - greeting sin mission
   - capability query sin mission
   - task creando mission
   - ejecución real
   - fallo
   - replanning
   - approval
   - missing capability
   - uncertainty
   - false success
   - recovery

22. End-to-End
   Probar el ciclo completo con herramientas reales disponibles.

==================================================
REGLAS DE SEGURIDAD
==================================================

Nunca permitir:

MODEL → EXECUTE

Siempre:

MODEL → DECISION → POLICY → GATE → EXECUTE

Nunca:

MODEL CLAIM → FACT

Siempre:

CLAIM → EVIDENCE → CLASSIFICATION

Nunca:

ACTION SUCCESS → OBJECTIVE SUCCESS

Siempre:

ACTION → OBSERVATION → EVALUATION → VERIFICATION

La Policy y los Gates son la autoridad.
El modelo no puede modificar sus propios permisos, autoridad o reglas de seguridad.

==================================================
DEFINICIÓN DE 100%
==================================================

P0 solo puede declararse 100% cuando:

- todo requisito obligatorio está implementado
- todo requisito está integrado
- los tests pasan
- los escenarios E2E funcionan
- existe evidencia real
- existe verificación
- existe persistencia
- existe recuperación
- existe auditabilidad
- no existen GAPs críticos
- no existen mocks sustituyendo funcionalidades que deberían ser reales

Si algo está incompleto, P0 NO está al 100%.

==================================================
PROTOCOLO DE TRABAJO
==================================================

FASE 1:
AUDITAR P0.

FASE 2:
Crear:
docs/P0-COGNITIVE-CORE-GAP-ANALYSIS.md

FASE 3:
Mostrar los GAPs ordenados por impacto.

FASE 4:
Implementar el GAP de mayor impacto.

FASE 5:
Ejecutar tests.

FASE 6:
Ejecutar E2E.

FASE 7:
Verificar que no rompiste funcionalidades existentes.

FASE 8:
Actualizar documentación.

FASE 9:
Continuar con el siguiente GAP.

REPETIR HASTA P0 = 100%.

NO comenzar P1.

NO implementar todavía avatar final, IoT, visión avanzada, navegador avanzado,
MCP masivo ni otras capacidades de fases posteriores salvo que sean estrictamente
necesarias para cerrar un requisito de P0.

==================================================
IMPORTANTE
==================================================

NO me entregues código todavía.

Primero haz únicamente el AUDIT de P0 y crea:

docs/P0-COGNITIVE-CORE-GAP-ANALYSIS.md

Después detente y dame un reporte con:

- estado actual de P0
- COMPLETE
- PARTIAL
- MISSING
- BROKEN
- porcentaje real calculado
- GAP de mayor impacto
- plan exacto para cerrarlo

Después de ese reporte podremos comenzar la implementación.

El objetivo final no es que ALEXIS parezca inteligente.

El objetivo es que ALEXIS realmente pueda:

ENTENDER → RAZONAR → DECIDIR → ACTUAR → OBSERVAR → EVALUAR → REPLANIFICAR → VERIFICAR → APRENDER → RECORDAR.

Empieza AHORA exclusivamente con el AUDIT de P0.
