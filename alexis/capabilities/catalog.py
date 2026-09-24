"""Catálogo y registro de capacidades de ALEXIS (F1 — Modelado).

CAPABILITY = lo que ALEXIS puede hacer (definido por el sistema). El registro es la
única fuente de `available_capabilities`/`required_capabilities` del Self Model.

Regla de diseño (docs/AUTONOMY-V0.5-CAPABILITIES.md §2): una capacidad NO se declara
disponible hasta que tiene adaptador real, sandbox de perfil y política. Por eso el
catálogo incluye capacidades futuras con `status="missing"` (sin habilitar) y ALEXIS
nunca finge tenerlas.
"""

from dataclasses import dataclass, field


@dataclass
class CapabilitySpec:
    id: str
    sphere: str
    network: bool
    side_effects: bool
    trust_domain: str
    sandbox_profile: str
    default_risk: str = "low"
    audit: str = "full"
    plans_action: str | None = None
    requires_input: str | None = None
    status: str = "available"  # available | base | missing
    resources: list[str] = field(default_factory=list)
    description: str = ""


class CapabilityRegistry:
    def __init__(self):
        self._specs: dict[str, CapabilitySpec] = {}
        self._enabled: set[str] = set()

    def register(self, spec: CapabilitySpec) -> None:
        if spec.id in self._specs:
            raise ValueError(f"Capability already registered: {spec.id}")
        self._specs[spec.id] = spec

    def register_all(self, specs) -> None:
        for spec in specs:
            self.register(spec)

    def has(self, capability_id: str) -> bool:
        return capability_id in self._specs

    def get(self, capability_id: str) -> CapabilitySpec:
        return self._specs[capability_id]

    def specs(self) -> list[CapabilitySpec]:
        return list(self._specs.values())

    def enable(self, *capability_ids: str) -> None:
        for cid in capability_ids:
            if cid not in self._specs:
                raise ValueError(f"Capability not registered: {cid}")
            self._enabled.add(cid)

    def disable(self, *capability_ids: str) -> None:
        for cid in capability_ids:
            self._enabled.discard(cid)

    def is_enabled(self, capability_id: str) -> bool:
        return capability_id in self._specs and capability_id in self._enabled

    def enabled(self) -> list[CapabilitySpec]:
        return [self._specs[cid] for cid in self._specs if cid in self._enabled]

    def available(self) -> list[CapabilitySpec]:
        return [s for s in self._specs.values() if s.status in ("available", "base")]


def build_catalog(enable_available=True) -> CapabilityRegistry:
    """Catálogo completo (estado honesto) + registro.

    Solo las capacidades con adaptador real se marcan `available`; las futuras
    quedan registradas como `missing` (invisibles para `available_capabilities`).
    """
    registry = CapabilityRegistry()
    registry.register_all(
        [
            CapabilitySpec(
                id="fs.read",
                sphere="filesystem",
                network=False,
                side_effects=False,
                trust_domain="filesystem",
                sandbox_profile="sandbox-project",
                default_risk="low",
                plans_action="research",
                resources=["fs.read"],
                description="Leer archivos dentro del perímetro autorizado",
            ),
            CapabilitySpec(
                id="fs.stat",
                sphere="filesystem",
                network=False,
                side_effects=False,
                trust_domain="filesystem",
                sandbox_profile="sandbox-project",
                default_risk="low",
                plans_action="verify",
                resources=["fs.stat"],
                description="Estatificar rutas dentro del perímetro autorizado",
            ),
            CapabilitySpec(
                id="fs.write",
                sphere="filesystem",
                network=False,
                side_effects=True,
                trust_domain="filesystem",
                sandbox_profile="sandbox-project",
                default_risk="medium",
                plans_action="modify",
                requires_input="ruta y contenido",
                resources=["fs.write"],
                description="Crear/editar archivos dentro del perímetro autorizado",
            ),
            CapabilitySpec(
                id="fs.remove",
                sphere="filesystem",
                network=False,
                side_effects=True,
                trust_domain="filesystem",
                sandbox_profile="sandbox-project",
                default_risk="critical",
                plans_action="remove",
                requires_input="ruta exacta a borrar",
                resources=["fs.remove"],
                description="Borrar archivos; siempre bajo política/approval",
            ),
            CapabilitySpec(
                id="cognition.understand",
                sphere="cognition",
                network=False,
                side_effects=False,
                trust_domain="self",
                sandbox_profile="none",
                default_risk="low",
                plans_action="understand",
                description="Comprender el objetivo y el contexto",
            ),
            CapabilitySpec(
                id="cognition.analyze",
                sphere="cognition",
                network=False,
                side_effects=False,
                trust_domain="self",
                sandbox_profile="none",
                default_risk="low",
                plans_action="analyze",
                description="Analizar evidencia y resultados",
            ),
            CapabilitySpec(
                id="research.filesystem",
                sphere="cognition",
                network=False,
                side_effects=False,
                trust_domain="filesystem",
                sandbox_profile="sandbox-project",
                default_risk="low",
                plans_action="research",
                resources=["fs.read", "fs.stat"],
                description="Investigar en el perímetro autorizado",
            ),
            CapabilitySpec(
                id="execution.sandbox",
                sphere="execution",
                network=False,
                side_effects=True,
                trust_domain="self",
                sandbox_profile="sandbox-project",
                default_risk="medium",
                plans_action="execute",
                description="Ejecutar pasos reales bajo sandbox",
            ),
            CapabilitySpec(
                id="execute.test",
                sphere="execution",
                network=False,
                side_effects=False,
                trust_domain="self",
                sandbox_profile="sandbox-project",
                default_risk="low",
                plans_action="test",
                status="base",
                description="Ejecutar pruebas deterministas (para F1.1)",
            ),
            CapabilitySpec(
                id="verification.filesystem",
                sphere="verification",
                network=False,
                side_effects=False,
                trust_domain="filesystem",
                sandbox_profile="none",
                default_risk="low",
                plans_action="verify",
                description="Verificación independiente de efectos sobre el workspace",
            ),
            CapabilitySpec(
                id="desktop.tools",
                sphere="desktop",
                network=True,
                side_effects=True,
                trust_domain="host",
                sandbox_profile="host-delegated",
                default_risk="medium",
                plans_action="execute",
                resources=["chrome.open_url", "spotify.play", "claude.open", "binance.open", "cursor.open"],
                description="Acciones de escritorio delegadas al host",
            ),
            CapabilitySpec(
                id="tts.speak",
                sphere="voice",
                network=True,
                side_effects=False,
                trust_domain="host",
                sandbox_profile="host-delegated",
                default_risk="low",
                plans_action="respond",
                resources=["tts.speak"],
                description="Voz saliente (síntesis de respuesta)",
            ),
            CapabilitySpec(
                id="perception.clap",
                sphere="perception",
                network=False,
                side_effects=False,
                trust_domain="self",
                sandbox_profile="none",
                default_risk="low",
                description="Percepción de palmada como fuente de eventos",
            ),
            CapabilitySpec(
                id="autonomy.gates",
                sphere="autonomy",
                network=False,
                side_effects=False,
                trust_domain="self",
                sandbox_profile="none",
                default_risk="low",
                description="Puertas de nivel de autonomía (assist/supervised/autonomous)",
            ),
            CapabilitySpec(
                id="autonomy.queue",
                sphere="autonomy",
                network=False,
                side_effects=False,
                trust_domain="self",
                sandbox_profile="none",
                default_risk="low",
                description="Cola FIFO persistente y reanudación por checkpoint",
            ),
            # ------ Capacidades futuras (honestamente NO disponibles) ------
            CapabilitySpec(
                id="git.read", sphere="project", network=False, side_effects=False,
                trust_domain="git", sandbox_profile="sandbox-project", default_risk="low",
                plans_action="read", status="missing", requires_input="repositorio/perímetro",
                description="Leer repositorios git locales (pendiente)",
            ),
            CapabilitySpec(
                id="git.commit", sphere="project", network=False, side_effects=True,
                trust_domain="git", sandbox_profile="sandbox-project", default_risk="high",
                plans_action="commit", status="missing", requires_input="mensaje firmado",
                description="Confirmar cambios (pendiente)",
            ),
            CapabilitySpec(
                id="git.push", sphere="project", network=True, side_effects=True,
                trust_domain="git", sandbox_profile="network-observed", default_risk="critical",
                plans_action="commit", status="missing",
                description="Publicar cambios (pendiente, requiere approval)",
            ),
            CapabilitySpec(
                id="terminal.run", sphere="project", network=False, side_effects=True,
                trust_domain="self", sandbox_profile="sandbox-terminal", default_risk="medium",
                plans_action="execute", status="missing",
                description="Subprocess con whitelist (pendiente)",
            ),
            CapabilitySpec(
                id="browser.research", sphere="web", network=True, side_effects=False,
                trust_domain="web", sandbox_profile="browser-sandbox", default_risk="medium",
                plans_action="browser", status="missing",
                description="Investigación web headless (pendiente)",
            ),
            CapabilitySpec(
                id="api.http", sphere="integration", network=True, side_effects=False,
                trust_domain="api", sandbox_profile="network-observed", default_risk="high",
                plans_action="browser", status="missing",
                description="APIs externas delegadas (pendiente)",
            ),
            CapabilitySpec(
                id="mcp.run", sphere="integration", network=True, side_effects=True,
                trust_domain="mcp", sandbox_profile="network-observed", default_risk="high",
                plans_action="execute", status="missing",
                description="Model Context Protocol (pendiente)",
            ),
            CapabilitySpec(
                id="vision.screen", sphere="perception", network=False, side_effects=False,
                trust_domain="self", sandbox_profile="browser-sandbox", default_risk="medium",
                plans_action="observe", status="missing",
                description="Visión/screen (pendiente)",
            ),
            CapabilitySpec(
                id="speech.stt", sphere="voice", network=True, side_effects=False,
                trust_domain="host", sandbox_profile="none", default_risk="low",
                plans_action="listen", status="missing",
                description="Voz entrante (requiere mic externo; mic interno roto)",
            ),
            CapabilitySpec(
                id="iot.mqtt", sphere="physical", network=True, side_effects=False,
                trust_domain="iot", sandbox_profile="network-observed", default_risk="critical",
                plans_action="observe", status="missing",
                description="IoT/MQTT/Home Assistant (pendiente F5)",
            ),
            CapabilitySpec(
                id="codex.run", sphere="integration", network=True, side_effects=True,
                trust_domain="codex", sandbox_profile="network-observed", default_risk="high",
                plans_action="execute", status="missing",
                description="Codex/agentes de código (pendiente F3)",
            ),
            CapabilitySpec(
                id="opencode.run", sphere="integration", network=True, side_effects=True,
                trust_domain="opencode", sandbox_profile="network-observed", default_risk="high",
                plans_action="execute", status="missing",
                description="OpenCode (pendiente F3)",
            ),
            CapabilitySpec(
                id="obsidian.run", sphere="integration", network=False, side_effects=True,
                trust_domain="obsidian", sandbox_profile="project", default_risk="medium",
                plans_action="modify", status="missing",
                description="Obsidian (pendiente F3)",
            ),
            CapabilitySpec(
                id="omniroute.run", sphere="integration", network=True, side_effects=True,
                trust_domain="omniroute", sandbox_profile="network-observed", default_risk="high",
                plans_action="execute", status="missing",
                description="OmniRoute (pendiente F3)",
            ),
            CapabilitySpec(
                id="hermes.run", sphere="integration", network=True, side_effects=True,
                trust_domain="hermes", sandbox_profile="network-observed", default_risk="high",
                plans_action="execute", status="missing",
                description="Hermes (pendiente F3)",
            ),
        ]
    )
    if enable_available:
        registry.enable(*(s.id for s in registry.specs() if s.status == "available"))
    return registry


# Acción del plan -> capability canónica (vocabulario compartido con el Self Model).
ACTION_TO_CAPABILITY = {
    "understand": "cognition.understand",
    "analyze": "cognition.analyze",
    "review": "cognition.analyze",
    "research": "research.filesystem",
    "read": "fs.read",
    "observe": "research.filesystem",
    "write": "fs.write",
    "modify": "fs.write",
    "remove": "fs.remove",
    "test": "execute.test",
    "commit": "git.commit",
    "verify": "verification.filesystem",
    "respond": "tts.speak",
    "execute": "execution.sandbox",
    "browser": "browser.research",
    "listen": "speech.stt",
}