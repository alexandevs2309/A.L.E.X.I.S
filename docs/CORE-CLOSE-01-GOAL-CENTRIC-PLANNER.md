# CORE-CLOSE-01 — Goal-Centric Planner

## Objetivo

Cerrar el primer incremento de P0 posterior al replanning dinámico: el plan deja de ser
solamente una secuencia de acciones y pasa a transportar explícitamente el propósito,
resultado esperado y criterio verificable de cada paso.

## Contrato por paso

Cada `PlanStep` debe declarar:

- `objective`: qué subobjetivo intenta conseguir el paso.
- `capability`: qué capacidad necesita.
- `action`: operación cognitiva/ejecutable.
- `args`: argumentos propuestos.
- `depends_on`: dependencias del DAG.
- `expected`: qué observación se espera producir.
- `success_criteria`: cómo se reconoce el éxito del paso.
- `risk`: riesgo declarado.
- `requires_approval`: requisito de autorización.

La misión mantiene además sus propios `success_criteria`; el paso final de verificación los
hereda cuando existen. Esto evita confundir el éxito de una acción con el éxito del objetivo.

## Autoridad

El planner solamente propone. El flujo sigue siendo:

`ModelPlanner → PlanValidator → CognitiveRuntime → Policy → Gate → Executor → Observation → Evaluation → GoalVerifier`

El modelo no recibe autoridad adicional por declarar un plan.

## Persistencia

El contrato de los pasos se serializa junto al plan existente mediante `plan_to_dict` /
`plan_from_dict`, por lo que checkpoint/resume conserva el objetivo y criterios del paso.

## Estado

Este incremento cubre el **contrato explícito** y su persistencia. No declara todavía cerrado
el planner goal-centric completo: quedan por demostrar la evaluación de éxito de cada paso,
la ejecución de estrategias multi-etapa realmente dinámicas y la recuperación E2E completa.
