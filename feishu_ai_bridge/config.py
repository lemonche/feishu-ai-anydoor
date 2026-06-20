import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict

try:
    import yaml
except ImportError:
    yaml = None

BASE_DIR = Path(__file__).parent.parent.resolve()
SETTINGS_FILE = BASE_DIR / "settings.yaml"


def _load_settings():
    defaults = {
        "feishu": {
            "lark_cli": "/usr/local/Cellar/node/25.9.0_1/lib/node_modules/@larksuite/cli/bin/lark-cli",
            "my_open_id": "ou_your_open_id_here",
            "chat_id": "oc_your_chat_id_here",
        },
        "active_backend": "trae",
        "backends": {
            "trae": {
                "path": "~/.local/bin/traecli",
                "session_id": "feishu-bridge-session-001",
                "timeout": 300,
                "yolo": True,
                "config_overrides": [],
            },
            "qwen": {
                "path": "qwen",
                "session_id": "feishu-bridge-qwen-001",
                "timeout": 300,
                "yolo": False,
                "config_overrides": [],
            },
        },
        "bridge": {
            "poll_interval": 0.5,
            "status_update_interval": 10,
            "command_prefix": "/",
            "max_queue_size": 10,
            "health_check_interval": 60,
        },
        "profiles_dir": "profiles",
        "active_profile": "default",
    }

    if yaml and SETTINGS_FILE.exists():
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            user_settings = yaml.safe_load(f) or {}
        for key in defaults:
            if key in user_settings:
                if isinstance(defaults[key], dict) and not isinstance(defaults[key], list):
                    defaults[key].update(user_settings[key])
                else:
                    defaults[key] = user_settings[key]
    return defaults


def _load_profiles(profiles_dir_name):
    profiles = {}
    profiles_dir = BASE_DIR / profiles_dir_name
    if not profiles_dir.exists():
        return profiles

    for profile_file in sorted(profiles_dir.glob("*.yaml")):
        name = profile_file.stem
        try:
            with open(profile_file, "r", encoding="utf-8") as f:
                profile_data = yaml.safe_load(f) or {}
            profile_data["_file"] = str(profile_file)
            profiles[name] = profile_data
        except Exception as e:
            print(f"[WARN] 加载 profile 失败 {profile_file}: {e}")
    return profiles


_settings = _load_settings()

_feishu = _settings["feishu"]
_bridge = _settings["bridge"]

# 向后兼容：旧的 traeci 顶层配置合并到 backends.trae
if "traecli" in _settings and isinstance(_settings["traecli"], dict):
    _settings.setdefault("backends", {})
    _settings["backends"].setdefault("trae", {})
    for k, v in _settings["traecli"].items():
        if k not in _settings["backends"]["trae"]:
            _settings["backends"]["trae"][k] = v

ACTIVE_BACKEND = _settings.get("active_backend", "trae")
BACKENDS = _settings.get("backends", {})

_backend_cfg = BACKENDS.get(ACTIVE_BACKEND, BACKENDS.get("trae", {}))

LARK_CLI = _feishu["lark_cli"]
MY_OPEN_ID = _feishu["my_open_id"]
CHAT_ID = _feishu["chat_id"]

# 通用后端配置（保留旧变量名以最小化改动）
TRAECLI = os.path.expanduser(_backend_cfg.get("path", "~/.local/bin/traecli"))
SESSION_ID = _backend_cfg.get("session_id", "feishu-bridge-session-001")
SESSION_TIMEOUT_SECONDS = _backend_cfg.get("session_timeout_seconds", 14400)
TRAECLI_TIMEOUT = _backend_cfg.get("timeout", 300)
TRAECLI_YOLO = _backend_cfg.get("yolo", True)
TRAECLI_CONFIG_OVERRIDES = _backend_cfg.get("config_overrides", [])

POLL_INTERVAL = _bridge["poll_interval"]
STATUS_UPDATE_INTERVAL = _bridge["status_update_interval"]
COMMAND_PREFIX = _bridge["command_prefix"]
MAX_QUEUE_SIZE = _bridge["max_queue_size"]
HEALTH_CHECK_INTERVAL = _bridge["health_check_interval"]

EVENTS_DIR = BASE_DIR / ".feishu_events"
PROCESSED_DIR = BASE_DIR / ".feishu_events_processed"
LOG_DIR = BASE_DIR / "logs"
LOG_MAX_BYTES = 10 * 1024 * 1024  # 10MB
LOG_BACKUP_COUNT = 5
PID_FILE = BASE_DIR / ".bridge.pid"

PROFILES = _load_profiles(_settings.get("profiles_dir", "profiles"))
ACTIVE_PROFILE = _settings.get("active_profile", "default")


def get_active_profile():
    return PROFILES.get(ACTIVE_PROFILE, PROFILES.get("default", {}))


def get_profile(name):
    return PROFILES.get(name, None)


def list_profiles():
    return {k: v.get("description", "") for k, v in PROFILES.items()}


def set_active_profile(name):
    global ACTIVE_PROFILE
    if name in PROFILES:
        ACTIVE_PROFILE = name
        _persist_setting("active_profile", name)
        return True
    return False


def set_active_backend(name):
    global ACTIVE_BACKEND, _backend_cfg, TRAECLI, SESSION_ID, SESSION_TIMEOUT_SECONDS, TRAECLI_TIMEOUT, TRAECLI_YOLO, TRAECLI_CONFIG_OVERRIDES
    if name not in BACKENDS:
        return False
    ACTIVE_BACKEND = name
    _backend_cfg = BACKENDS[name]
    TRAECLI = os.path.expanduser(_backend_cfg.get("path", "~/.local/bin/traecli"))
    SESSION_ID = _backend_cfg.get("session_id", "feishu-bridge-session-001")
    SESSION_TIMEOUT_SECONDS = _backend_cfg.get("session_timeout_seconds", 14400)
    TRAECLI_TIMEOUT = _backend_cfg.get("timeout", 300)
    TRAECLI_YOLO = _backend_cfg.get("yolo", True)
    TRAECLI_CONFIG_OVERRIDES = _backend_cfg.get("config_overrides", [])
    _persist_setting("active_backend", name)
    return True


def _persist_setting(key, value):
    if not yaml or not SETTINGS_FILE.exists():
        return
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        data[key] = value
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            yaml.dump(data, f, allow_unicode=True, default_flow_style=False)
    except Exception as e:
        print(f"[WARN] 持久化 {key} 失败: {e}")

def validate_startup():
    errors = []
    warnings = []

    if not Path(TRAECLI).exists():
        errors.append(f"traecli 路径不存在: {TRAECLI}")
    elif not os.access(TRAECLI, os.X_OK):
        errors.append(f"traecli 无执行权限: {TRAECLI}")

    if not Path(LARK_CLI).exists():
        errors.append(f"lark-cli 路径不存在: {LARK_CLI}")
    elif not os.access(LARK_CLI, os.X_OK):
        errors.append(f"lark-cli 无执行权限: {LARK_CLI}")

    if yaml is None:
        errors.append("PyYAML 未安装，无法加载配置 (pip3 install pyyaml)")

    if not SETTINGS_FILE.exists():
        warnings.append("settings.yaml 不存在，使用默认配置")

    if not PROFILES:
        warnings.append("未加载到任何 Profile 配置")
    else:
        for name, profile in PROFILES.items():
            if not profile.get("model"):
                warnings.append(f"Profile '{name}' 未配置 model")

    return errors, warnings


# ── AppContext: centralized mutable state ──────────────────────────


@dataclass
class AppContext:
    """Centralized mutable application state, replacing module-level globals."""
    start_time: float = 0.0
    event_proc: object = None
    session_pool: object = None
    task_queues: Dict[str, object] = field(default_factory=dict)
    task_queues_lock: threading.Lock = field(default_factory=threading.Lock)
    running: bool = True
    cli_version_cache: str = ""


_app_context = AppContext()


def get_app_context() -> AppContext:
    return _app_context
