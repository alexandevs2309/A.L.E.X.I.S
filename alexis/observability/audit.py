from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass
class AuditRecord:
    event: str
    actor: str
    mission_id: str | None
    details: dict


class AuditLog:
    def __init__(self):
        self.records = []

    def record(self, event, actor, mission_id=None, **details):
        self.records.append(AuditRecord(
            event=event,
            actor=actor,
            mission_id=mission_id,
            details={"timestamp": datetime.now(timezone.utc).isoformat(), **details},
        ))
