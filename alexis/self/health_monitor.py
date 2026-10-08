"""Health y model_state del Self Model, derivados SOLO de eventos reales del runtime.

Ventana deslizante de 5 minutos (`deque`): métricas reseteables, sin prom/telegrafía.
"""

from __future__ import annotations

import time
from collections import deque
from typing import Any


class HealthIssue:
    __slots__ = ("code", "detail", "at")

    def __init__(self, code: str, detail: str, at: float) -> None:
        self.code = code
        self.detail = detail
        self.at = at

    def to_dict(self) -> dict:
        return {"code": self.code, "detail": self.detail, "at": self.at}


class HealthMonitor:
    def __init__(self, *, window_seconds: float = 300.0, clock=time.monotonic) -> None:
        self.window = window_seconds
        self._clock = clock
        self._events: deque[tuple[str, float, dict]] = deque()

    def record(self, topic: str, payload: Any) -> None:
        self._events.append((topic, self._clock(), dict(payload or {})))
        self._prune()

    def reset(self) -> None:
        self._events.clear()

    def _prune(self) -> None:
        cutoff = self._clock() - self.window
        while self._events and self._events[0][1] < cutoff:
            self._events.popleft()

    def _recent(self) -> list[tuple[str, float, dict]]:
        self._prune()
        return list(self._events)

    @staticmethod
    def _payload(e) -> dict:
        return e[2]

    def model_state(self) -> dict:
        recent = self._recent()
        routed = [e[2] for e in recent if e[0] == "model.routed"]
        failed: list[str] = []
        for e in recent:
            if e[0] in ("tool.failed", "mission.failed"):
                failed.append(e[0])
        total = max(1, len(recent))
        latencies = [v for _, _, p in recent if (v := p.get("latency_ms")) is not None]
        latencies = [float(v) for v in latencies if isinstance(v, (int, float))]
        latencies.sort()
        latency_p50 = (latencies[len(latencies) // 2] if latencies else 0.0)

        outcomes = [p.get("outcome") for p in routed]
        streak = 0
        for out in reversed(outcomes):
            if out == "degraded":
                streak += 1
            else:
                break

        providers = [p.get("provider") for p in routed if p.get("provider")]
        current = providers[-1] if providers else None
        return {
            "current_provider": current,
            "provenance_history": providers[-10:],
            "latency_p50_ms": latency_p50,
            "error_rate_5min": round(len(failed) / total, 4),
            "degraded_streak": streak,
        }

    def health(self) -> dict:
        recent = self._recent()
        mission_failed = [e for e in recent if e[0] == "mission.failed"]
        tool_failed = [e for e in recent if e[0] == "tool.failed"]
        routed = [e[2] for e in recent if e[0] == "model.routed"]
        stalls = sum(1 for e in recent if e[0] == "cognition.stall")
        replans = sum(1 for e in recent if e[0] == "cognition.replan")

        outcomes = [p.get("outcome") for p in routed]
        unavailable = outcomes.count("unavailable")
        degraded_now = bool(outcomes and outcomes[-1] == "degraded")

        latencies = [p.get("latency_ms") for p in routed if isinstance(p.get("latency_ms"), (int, float))]
        latencies.sort()
        latency_p50 = float(latencies[len(latencies) // 2]) if latencies else 0.0
        error_rate = (len(mission_failed) + len(tool_failed)) / max(1, len(recent))

        issues: list[HealthIssue] = []
        now = self._clock()
        if len(mission_failed) >= 3:
            issues.append(HealthIssue("mission_health", f"{len(mission_failed)} misiones fallidas en {int(self.window)}s", now))
        if unavailable >= 3 or (outcomes and outcomes[-1] == "unavailable"):
            issues.append(HealthIssue("provider_unavailable", f"{unavailable} respuesta(s) UNAVAILABLE", now))
        if error_rate > 0.3:
            issues.append(HealthIssue("error_rate", f"error rate {error_rate:.0%} > 30%", now))
        if latency_p50 > 5000.0:
            issues.append(HealthIssue("latency", f"latency_p50 {latency_p50:.0f}ms > 5000ms", now))
        if degraded_now:
            issues.append(HealthIssue("provider_degraded", "provider en modo DEGRADED", now))
        if replans >= 3:
            issues.append(HealthIssue("repeated_replan", f"{replans} replans en la ventana", now))
        if stalls >= 2:
            issues.append(HealthIssue("repeated_stall", f"{stalls} bloqueos cognitivos en la ventana", now))

        if len(mission_failed) >= 3 or unavailable >= 3 or (outcomes and outcomes[-1] == "unavailable"):
            status = "CRITICAL"
        elif error_rate > 0.3 or latency_p50 > 5000.0:
            status = "WARNING"
        elif degraded_now:
            status = "DEGRADED"
        else:
            status = "HEALTHY"

        return {
            "status": status,
            "issues": [i.to_dict() for i in issues],
        }
