# ALEXIS Capability Map

> **v0.5:** cada dominio de este mapa se despliega como **CapabilitySpec** concreto
> (id, esfera, red, side_effects, trust_domain, sandbox_profile, riesgo, auditoría)
> gobernado por la Policy Engine. Ver `docs/AUTONOMY-V0.5-CAPABILITIES.md`.

## Cognitive
Reasoning, planning, decision decomposition, reflection, uncertainty and verification.

## Autonomous
Mission execution, scheduling, recovery, initiative, stop criteria and long-running work.

## Digital perception
Files, repositories, APIs, logs, databases, browser, screens and images.

## Physical perception
Future cameras, microphones, sensors and IoT telemetry.

## Action
Filesystem, terminal, Git/GitHub, browser, APIs, cloud services and IoT.

> `git.read` / `git.commit` / `git.push`: **IMPLEMENTED** (`alexis/tools/git.py`).
> `git.read` es sólo lectura y su whitelist de verbos (`status`, `log`, `diff`, `show`,
> `ls-files`) no incluye ningún verbo que escriba — es la razón de que exista el módulo y
> no tres llamadas más a `terminal`. `git.commit` (riesgo **high**, según catálogo)
> confirma sólo ficheros dentro del workspace: el sandbox resuelve y comprueba cada ruta
> antes de que git vea el nombre. `git.push` (riesgo **critical**) publica fuera de esta
> máquina, exige aprobación explícita siempre y **valida el remoto resuelto** antes de
> abrir el socket, porque el sandbox no aísla la red. Las credenciales no las ve el
> código: van en la configuración que git ya tenga en el repositorio.
>
> `browser.research`: **IMPLEMENTED** (`alexis/tools/browser.py`). Sólo lectura (`network_read`),
risk medium, sin aprobación, verificación `source_count`. Extracción de texto plano con
`html.parser`, sin JavaScript ni navegador headless. Allowlist `allowed_domains` + protección
SSRF + descarte de prompt injection. Aislamiento de red **a nivel de aplicación, no de
kernel** (`docs/SECURITY.md`).

## Multi-agent
Researcher, coder, QA, security, DevOps, browser, IoT and critic.

## Memory
Episodic, semantic, procedural, project context and knowledge graph.

## Learning
Experience evaluation, reflection, skill generation, experiments and controlled versioning.

## Communication
Voice, text, notifications and future mobile channels.

## Experience
Dynamic UI, avatar, 3D visualization, voice presence and context-aware visual surfaces.

## Physical world
MQTT, Home Assistant, sensors and actuators with explicit safety policies.

## Future intelligence
Local/cloud model routing, embeddings, multimodal models, ML, fine-tuning and reinforcement learning.
## Boundaries

ALEXIS does not get unlimited authority, unrestricted self-modification, or automatic permission to perform high-impact actions.

La seguridad es **contextual y granular**, no una restricción global: cada capacidad
tiene su política, su sandbox, su riesgo y su necesidad de aprobación evaluada en
tiempo de ejecución.

---

## Resolución de capabilities (R-01, 2026-10-13)

La resolución de una capability a una tool se hace en `alexis/execution.py::execute`, y hay
**tres** casos distintos. Antes sólo se distinguishan dos, y el caso que faltaba era el que
importaba:

1. La capability está en `_CAPABILITY_TOOL` → manda la tabla (`fs.*`, `execute.test`,
   `browser.research`).
2. La tool está registrada en el `ToolRegistry` aunque no esté en la tabla → **se ejecuta esa
   tool**. Antes esto no existía.
3. No hay tool con ese nombre → cae a la tabla por **acción**. Aquí viven los alias
   semánticos (`research.filesystem` ES leer el filesystem) y las capabilities internas del
   runtime (`cognition.*`, `autonomy.*`).

**Por qué el caso 2 era un defecto y no una decisión.** El catálogo declara `git.read` y
`git.commit` como `available`, ambas tienen adaptador real, `PlanValidator` las acepta porque
están disponibles y la autoridad las autoriza. Y entonces el paso se ejecutaba como
`fs.read`/`fs.write`, en silencio: el catálogo affirmed una capacidad, el paso pedía otra y
ocurría una tercera. Con `action=execute` y objetivo de escritura, `git.commit` terminaba
**escribiendo un archivo** — y podía reportar `success` sobre un commit que no se hizo.

El catálogo no podía detectarlo porque `available()` se apoya únicamente en `status`, y
`catalog.py` no tenía ni una mención de adapter, dispatch o executor: la disponibilidad se
declaraba sin que nadie comprobara que la capability fuera ejecutable.

**Cómo se evita la clase de defecto.** `tests/test_capability_resolution.py::test_04` recorre
todas las capabilities `available`/`base` que tienen adaptador registrado y las EJECUTA, una
con la capability declarada y otra con override explícito, y exige que coincidan. Es
comportamental a propósito: comprobar si el id está en una tabla mediría la
implementación, no el contrato, y pasaría en verde con el defecto vivo.

**Lo que sigue sin ser cierto.** `available` significa «hay implementación y está
registrada», no «se puede ejecutar en cualquier contexto»: `git.push` es `missing` aunque su
adaptador exista, porque la operación no funciona dentro del sandbox.
