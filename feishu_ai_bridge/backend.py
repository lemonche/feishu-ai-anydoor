"""Backend abstraction layer — unified interface for multiple AI CLI backends."""

from .traecli import run_traecli_streaming, cancel_current as traecli_cancel, get_current_pid as traecli_pid
from .qwencli import run_qwencli_streaming, cancel_current as qwencli_cancel, get_current_pid as qwencli_pid

BACKENDS = {
    "trae": {
        "name": "Trae CLI",
        "run": run_traecli_streaming,
        "cancel": traecli_cancel,
        "get_pid": traecli_pid,
    },
    "qwen": {
        "name": "Qwen Code",
        "run": run_qwencli_streaming,
        "cancel": qwencli_cancel,
        "get_pid": qwencli_pid,
    },
}

DEFAULT_BACKEND = "trae"


def get_backend(name):
    return BACKENDS.get(name)


def list_backends():
    return {k: v["name"] for k, v in BACKENDS.items()}


def run_backend(instruction, session_id, chat_id, backend_name=None):
    if backend_name is None:
        from .config import ACTIVE_BACKEND
        backend_name = ACTIVE_BACKEND
    backend = get_backend(backend_name)
    if not backend:
        return f"未知后端: {backend_name}", -1, {}
    return backend["run"](instruction, session_id, chat_id)


def cancel_backend(backend_name=None):
    if backend_name is None:
        from .config import ACTIVE_BACKEND
        backend_name = ACTIVE_BACKEND
    backend = get_backend(backend_name)
    if not backend:
        return False
    return backend["cancel"]()


def get_backend_pid(backend_name=None):
    if backend_name is None:
        from .config import ACTIVE_BACKEND
        backend_name = ACTIVE_BACKEND
    backend = get_backend(backend_name)
    if not backend:
        return None
    return backend["get_pid"]()
