"""Percepción de ALEXIS: interfaz, micrófono, detección de palmadas, wake word y activación."""

from alexis.perception.activation import (
    ACTIVATION_OBJECTIVE,
    WELCOME_REPLY,
    activation_reply,
    is_activation_objective,
)
from alexis.perception.clap_detector import ClapDetected, ClapDetector, rms_mono
from alexis.perception.clap_listener import ClapListener
from alexis.perception.microphone import (
    MicrophoneSelector,
    ProbeResult,
    choose_device_no_probe,
    default_device_index,
    env_input_device,
    input_devices,
    resolve_device_index,
)
from alexis.perception.wakeword import (
    WakewordMatch,
    find_wakeword,
    is_activation,
    normalize,
)

__all__ = [
    "ACTIVATION_OBJECTIVE",
    "WELCOME_REPLY",
    "activation_reply",
    "is_activation_objective",
    "ClapDetected",
    "ClapDetector",
    "ClapListener",
    "rms_mono",
    "MicrophoneSelector",
    "ProbeResult",
    "choose_device_no_probe",
    "default_device_index",
    "env_input_device",
    "input_devices",
    "resolve_device_index",
    "WakewordMatch",
    "find_wakeword",
    "is_activation",
    "normalize",
]