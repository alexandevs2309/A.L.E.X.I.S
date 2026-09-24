from abc import ABC, abstractmethod


class MemoryStore(ABC):
    @abstractmethod
    async def store_observation(self, mission_id: str, observation): ...

    @abstractmethod
    async def recall(self, query: str, limit: int = 10): ...


class InMemoryMemory(MemoryStore):
    def __init__(self):
        self.items = []

    async def store_observation(self, mission_id, observation):
        self.items.append((mission_id, observation))

    async def recall(self, query, limit=10):
        return self.items[-limit:]
