from typing import Protocol


class SpeechToText(Protocol):
    async def transcribe(self, audio): ...


class TextToSpeech(Protocol):
    async def synthesize(self, text: str): ...


class NotificationChannel(Protocol):
    async def send(self, message: str, priority: str = "normal"): ...
