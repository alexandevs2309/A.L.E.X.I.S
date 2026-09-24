from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable


@dataclass
class Tool:
    name: str
    description: str
    risk: str = "low"
    handler: Callable[..., Awaitable] | None = None
    schema: dict[str, Any] | None = None
    permissions: dict[str, Any] = field(default_factory=dict)
    timeout: float | None = None
    limits: dict[str, Any] = field(default_factory=dict)
    capability_id: str | None = None
    sandbox_profile: str | None = None


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool):
        if tool.name in self._tools:
            raise ValueError(f"Tool already registered: {tool.name}")
        self._tools[tool.name] = tool

    def register_all(self, tools):
        for tool in tools:
            self.register(tool)

    def get(self, name: str) -> Tool:
        return self._tools[name]

    def has(self, name: str) -> bool:
        return name in self._tools

    def list(self):
        return list(self._tools.values())