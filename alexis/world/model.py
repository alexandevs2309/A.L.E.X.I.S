from dataclasses import dataclass, field


@dataclass
class WorldEntity:
    id: str
    kind: str
    name: str
    attributes: dict = field(default_factory=dict)


class WorldModel:
    def __init__(self):
        self.entities: dict[str, WorldEntity] = {}

    def upsert(self, entity: WorldEntity):
        self.entities[entity.id] = entity

    def get(self, entity_id: str):
        return self.entities.get(entity_id)

    def snapshot(self):
        return list(self.entities.values())
