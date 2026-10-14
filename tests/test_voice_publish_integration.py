"""Integración de la ruta de voz: generar → publicar. Hueco ciego que esta prueba cubre.

La auditoría de §40 encontró que la ruta de voz del demo —`on_mission_end` →
`_announce_voice` → `synthesize_with_fallback` → `_publish_voice`— **no tenía ni una sola
prueba**. `test_speech_tts.py` (21 tests) cubre configuración de entorno, claves de caché y
fallos honestos de proveedor; nunca invoca un síntesis que funcione. `test_perception.py`
cubre selección de dispositivo y detección de palmada; nunca captura. El hueco estaba entre
ambos: la ruta que une las dos mitades no estaba probada por nadie.

**Lo que esta prueba NO demuestra, y no se afirma:**

- **No demuestra reproducción física.** Lo que se prueba es que el audio se *publica* en el
  directorio vigilado. Que suene por los altavoces depende del `voice_bridge` del host y de
  `aplay`/`gst-play`, que están fuera del proceso. Hay 52 MP3 reales en `.cache/tts/` con el
  patrón de nombres que se verifica aquí, lo que acredita ejecución real del runtime, pero no
  acredita que el sonido saliera por el altavoz.
- **No usa la red.** El proveedor se inyecta, y `synthesize_with_fallback` —que NO se
  sustituye— sólo intenta su fallback a Edge cuando el proveedor injected falla. En los
  caminos felices no se toca la red; en el de fallo se apaga `EdgeTTSProvider.available()`
  explícitamente para que el test sea hermético y rápido.

**Qué se sustituye y qué no.** Se sustituye `get_tts_provider()`, que es una fábrica: elegir
*qué* motor hablar es configuración. **No** se sustituye `synthesize_with_fallback`, ni
`_announce_voice`, ni `_publish_voice`, ni la composición del texto, ni la comprobación de
`voice_mode_on`. Todo eso es lo que se quiere demostrar, y sustituirlo sería tautológico.
"""

from __future__ import annotations

import asyncio
import importlib
import threading
import time

import pytest

from alexis.events.bus import EventBus
from alexis.speech.tts import TTSProvider, TTSResult

server = importlib.import_module("apps.demo.server")


class _ProveedorFalso(TTSProvider):
    """Proveedor de prueba: devuelve un audio REAL en disco, no una respuesta simulada.

    Se inyecta en la fábrica, no en la ruta. Escribe un fichero con bytes concretos para que
    la prueba pueda comprobar que lo publicado es exactamente lo sintetizado — si sólo se
    comprobara que «hay un fichero», pasaría igual con audio corrupto o vacío.
    """

    name = "prueba"
    BYTES = b"ID3\x03\x00\x00\x00\x00\x00\x00audio-de-prueba" + b"\xff\xfb" + b"\x00" * 64

    def __init__(self, destino, *, ok: bool = True):
        self.destino = destino
        self.ok = ok
        self.peticiones: list[str] = []

    async def synthesize(self, text, voice_id=None, model_id=None, fmt="pcm_24000"):
        self.peticiones.append(text)
        if not self.ok:
            return TTSResult(False, self.name, message="sin motor", error="sin motor")
        ruta = self.destino / f"sintetizado-{len(self.peticiones)}.mp3"
        ruta.write_bytes(self.BYTES)
        return TTSResult(True, self.name, message="ok", duration_ms=12, path=str(ruta))


@pytest.fixture
def voz(tmp_path, monkeypatch):
    """Entorno de voz aislado: caché propia, loop real en hilo y proveedor inyectado."""
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setenv("ALEXIS_TTS_CACHE_DIR", str(cache))

    loop = asyncio.new_event_loop()
    hilo = threading.Thread(target=loop.run_forever, daemon=True)
    hilo.start()
    try:
        yield cache, loop
    finally:
        loop.call_soon_threadsafe(loop.stop)
        hilo.join(timeout=5)
        loop.close()


class _Rt:
    """Lo mínimo que `_announce_voice` y `voice_mode_on` leen de un `OfficialRuntime`."""

    def __init__(self, loop):
        self.loop = loop
        self.events = EventBus()
        self.state = {"voice_mode": True, "voice_last_activity": time.time()}


def _mision(estado="completed"):
    """Misión en el estado indicado, por la vía que el propio sistema permite.

    `Mission.__setattr__` rechaza `COMPLETED` sin `GoalVerification` confirmado
    (`UnverifiedGoalError`), y con razón: es la invariante §4.6. Saltársela con
    `object.__setattr__` habría sido una prueba que roda por encima del control que el
    proyecto considera importante. Aquí se construye la verificación real que
    `goal_is_confirmed` exige —mismo objetivo, mismo `mission_id`, criterio satisfecho con
    evidencia fiable y de procedencia sana— y se deja que el guard la acepte.
    """
    from alexis.autonomy.mission import MissionEngine
    from alexis.cognition.goal_verification import (
        CriterionEvaluation,
        CriterionEvidence,
        CriterionStatus,
        GoalVerification,
    )
    from alexis.contracts import AutonomyLevel, MissionEnvelope, MissionState

    # El criterio se DECLARA en el envelope. `subject_matches` compara el `subject` de la
    # verificación contra `mission.goal.success_criteria`: si la misión no declara criterios,
    # una verificación que dice haberlos comprobado no puede pertainsele, y con razón.
    criterio = "el proyecto se ha revisado"
    envelope = MissionEnvelope(
        objective="revisar el proyecto",
        autonomy=AutonomyLevel.SUPERVISED,
        allowed_actions=["understand", "research", "respond"],
    )
    m = MissionEngine().create("revisar el proyecto", envelope, success_criteria=[criterio])

    if estado == "completed":
        # El `subject` se deriva con la misma función que usa el verificador, no a mano:
        # `subject_matches` compara contra `verification_subject(mission, criterios)` y un
        # subject escrito a ojo no casaría. Y el `source` de la evidencia debe llevar el
        # prefijo `tool:` — un claim del modelo no puede sostener un `satisfied`.
        from alexis.cognition.goal_verification import verification_subject
        m.goal_verification = GoalVerification(
            objective=m.goal.objective,
            verified=True,
            reason="revisado",
            evaluations=[
                CriterionEvaluation(
                    criterion=criterio,
                    status=CriterionStatus.SATISFIED,
                    reason="revisión completada",
                    evidence=[
                        CriterionEvidence(
                            evidence_id="ev-1",
                            source="tool:fs.read",
                            grade="evidence",
                            detail="el proyecto se leyó",
                            trusted=True,
                        )
                    ],
                )
            ],
            subject=verification_subject(m, [criterio]),
        )
        m.state = MissionState.COMPLETED
    else:
        m.state = MissionState(estado)
    return m


# --------------------------------------------------------------------------- #
# 1 — generación y publicación por la ruta real
# --------------------------------------------------------------------------- #


async def test_la_mision_publica_el_audio_sintetizado(voz, monkeypatch):
    """El recorrido completo: termina la misión → habla → el fichero queda publicado."""
    cache, loop = voz
    destino = cache / "origen"
    destino.mkdir()

    proveedor = _ProveedorFalso(destino)
    monkeypatch.setattr(server, "get_tts_provider", lambda: proveedor)

    rt = _Rt(loop)
    mission = _mision()

    await server._announce_voice(rt, mission)

    publicados = sorted(cache.glob("revisar-el-proyecto-*.mp3")) or sorted(cache.glob("*.mp3"))
    assert publicados, (
        f"_announce_voice terminó sin publicar nada. El proveedor recibió "
        f"{proveedor.peticiones!r} y el caché tiene: {[p.name for p in cache.iterdir()]}"
    )

    publicado = publicados[0]
    assert publicado.read_bytes() == _ProveedorFalso.BYTES, (
        "lo publicado no es el audio sintetizado: la copia alteró el contenido"
    )
    assert publicado.stat().st_size == len(_ProveedorFalso.BYTES)


async def test_el_nombre_cumple_el_patron_que_exige_el_bridge(voz, monkeypatch):
    """El bridge distingue audios por nombre: `mission_id-<milisegundos>.<ext>`.

    El sufijo de tiempo es lo que impide que el bridge salte el mismo audio si el texto se
    repite. Sin él, ALEXIS «diría» la misma frase y el host la ignoraría.
    """
    cache, loop = voz
    destino = cache / "origen"
    destino.mkdir()
    monkeypatch.setattr(server, "get_tts_provider", lambda: _ProveedorFalso(destino))

    await server._announce_voice(_Rt(loop), _mision())

    assert list(cache.glob("*.mp3")), "no se publicó ningún mp3"
    nombre = list(cache.glob("*.mp3"))[0].name
    prefijo, _, sufijo = nombre.rpartition("-")
    assert prefijo, f"el nombre no lleva mission_id antes del guion: {nombre!r}"
    assert sufijo.endswith(".mp3"), f"extensión inesperada: {nombre!r}"
    assert sufijo[:-4].isdigit(), (
        f"el sufijo no son milisegundos: {nombre!r}. El bridge lo usa para detectar audio nuevo"
    )


async def test_el_texto_sintetizado_es_el_que_afirma_el_estado(voz, monkeypatch):
    """No se prueba «que habló», sino que dijo lo que corresponde al desenlace real.

    Se comprueba contra el proveedor, que es quien recibe el texto. Un texto inventado o
    genérico también produciría audio; lo que no debe es afirmar un éxito que no ocurrió.
    """
    cache, loop = voz
    destino = cache / "origen"
    destino.mkdir()

    for estado, fragmento in (
        ("completed", "completé la misión"),
        ("failed", "No pude completar"),
        ("blocked", "No logré confirmar"),
    ):
        destino.mkdir(exist_ok=True)
        proveedor = _ProveedorFalso(destino)
        monkeypatch.setattr(server, "get_tts_provider", lambda p=proveedor: p)
        await server._announce_voice(_Rt(loop), _mision(estado))
        assert proveedor.peticiones, f"con estado={estado} no se sintetizó nada"
        assert fragmento in proveedor.peticiones[-1], (
            f"estado={estado}: el texto no corresponde al desenlace: "
            f"{proveedor.peticiones[-1]!r}"
        )


# --------------------------------------------------------------------------- #
# 2 — la ruta que reutiliza audio ya sintetizado
# --------------------------------------------------------------------------- #


async def test_reutiliza_el_audio_ya_sintetizado_en_el_paso_respond(voz, monkeypatch):
    """Si el paso ya traía su TTS, se publica ese; no se sintetiza dos veces.

    Es la ruta que evita el doble gasto: el `action="respond"` del executor ya generó el
    audio. Si aquí se volviera a sintetizar, cada respuesta se diría dos veces.
    """
    cache, loop = voz
    destino = cache / "origen"
    destino.mkdir()
    origen = destino / "ya-sintetizado.mp3"
    origen.write_bytes(_ProveedorFalso.BYTES)

    proveedor = _ProveedorFalso(destino)
    monkeypatch.setattr(server, "get_tts_provider", lambda: proveedor)

    mission = _mision()
    mission.results = [
        {"output": {"action": "respond", "tts": {"ok": True, "path": str(origen)}}}
    ]

    await server._announce_voice(_Rt(loop), mission)

    assert proveedor.peticiones == [], (
        f"sintetizó otra vez pese a haber audio listo: {proveedor.peticiones!r}"
    )
    assert list(cache.glob("*.mp3")), "el audio ya sintetizado no se publicó"


# --------------------------------------------------------------------------- #
# 3 — que no se publique lo que no se debe
# --------------------------------------------------------------------------- #


async def test_no_publica_si_el_modo_voz_esta_apagado(voz, monkeypatch):
    """El modo voz es una condición de la ruta, no un detalle.

    Con la voz apagada no se sintetiza ni se publica: el usuario la ha silenciado.
    """
    cache, loop = voz
    destino = cache / "origen"
    destino.mkdir()
    proveedor = _ProveedorFalso(destino)
    monkeypatch.setattr(server, "get_tts_provider", lambda: proveedor)

    rt = _Rt(loop)
    rt.state["voice_mode"] = False

    await server._announce_voice(rt, _mision())

    assert proveedor.peticiones == [], "sintetizó con la voz apagada"
    assert not list(cache.glob("*.mp3")), "publicó audio con la voz apagada"


async def test_no_publica_si_la_mision_no_ha_terminado(voz, monkeypatch):
    """Sólo se habla al terminar. Una misión en curso no se anuncia."""
    cache, loop = voz
    destino = cache / "origen"
    destino.mkdir()
    proveedor = _ProveedorFalso(destino)
    monkeypatch.setattr(server, "get_tts_provider", lambda: proveedor)

    await server._announce_voice(_Rt(loop), _mision("running"))

    assert proveedor.peticiones == [], "sintetizó con la misión en curso"
    assert not list(cache.glob("*.mp3")), "publicó audio con la misión en curso"


async def test_un_proveedor_que_falla_no_publica_nada_falso(voz, monkeypatch):
    """Si el motor falla, no se publica un fichero vacío ni se finge que habló.

    Se apaga el fallback a Edge para que la prueba sea hermética: lo que se comprueba es que
    la ruta **no inventa** salida cuando el proveedor honestamente falla.
    """
    cache, loop = voz
    destino = cache / "origen"
    destino.mkdir()
    proveedor = _ProveedorFalso(destino, ok=False)
    monkeypatch.setattr(server, "get_tts_provider", lambda: proveedor)
    monkeypatch.setattr(
        "alexis.speech.edge.EdgeTTSProvider.available", staticmethod(lambda: False)
    )

    await server._announce_voice(_Rt(loop), _mision())

    assert proveedor.peticiones, "ni siquiera intentó sintetizar"
    assert not list(cache.glob("*.mp3")), (
        "publicó audio pese a que el proveedor falló: la ruta está fingiendo que habló"
    )


# --------------------------------------------------------------------------- #
# 4 — la publicación por sí misma
# --------------------------------------------------------------------------- #


def test_publish_voice_rechaza_un_origen_inexistente(voz):
    """No inventar el fichero que no existe: `_publish_voice` devuelve False."""
    cache, _ = voz
    assert server._publish_voice(str(cache / "no-existe.mp3"), "m1") is False
    assert not list(cache.glob("*.mp3"))


def test_publish_voice_copia_los_mismos_bytes(voz):
    """La copia debe ser íntegra: el bridge reproduce lo que hay en disco."""
    cache, _ = voz
    origen = cache / "origen.mp3"
    origen.write_bytes(b"\x00" * 10 + b"contenido real")

    publicado = server._publish_voice(str(origen), "mision-de-prueba")
    assert publicado is True, "_publish_voice devolvió False sobre un fichero existente"

    copiados = list(cache.glob("mision-de-prueba-*.mp3"))
    assert copiados, f"no hay copia publicada: {[p.name for p in cache.iterdir()]}"
    assert copiados[0].read_bytes() == origen.read_bytes(), (
        "la copia alteró los bytes: el bridge reproduciría algo distinto de lo sintetizado"
    )