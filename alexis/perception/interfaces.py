from typing import Protocol


class Vision(Protocol):
    async def analyze_image(self, image): ...


class ScreenPerception(Protocol):
    async def inspect(self, screenshot): ...


class AudioPerception(Protocol):
    async def transcribe(self, audio): ...


class SensorPerception(Protocol):
    async def read(self, device_id: str): ...
