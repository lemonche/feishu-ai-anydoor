import os
import signal
import subprocess
import sys
import time

from .config import COMMAND_PREFIX, list_profiles, get_active_profile, set_active_profile, ACTIVE_PROFILE
from .feishu import log_info, log_warn, log_error, send_reply, send_feishu_markdown, _smart_truncate
from .i18n import t

_service_start_time = None


def set_service_start_time(t_val):
    global _service_start_time
    _service_start_time = t_val


def parse_message(content):
    content = content.strip()
    if content.startswith(COMMAND_PREFIX):
        rest = content[len(COMMAND_PREFIX):].strip()
        parts = rest.split(None, 1)
        cmd = parts[0].lower() if parts else ""
        args = parts[1] if len(parts) > 1 else ""
        return rest, True, cmd, args
    return content, False, "", ""


def handle_builtin_command(cmd, args, chat_id, session_pool, task_queues):
    if cmd == "help":
        send_feishu_markdown(t("help.content"), chat_id)
        return True

    elif cmd == "status":
        return _handle_status(chat_id, session_pool, task_queues)

    elif cmd == "reset":
        return _handle_reset(chat_id, session_pool)

    elif cmd == "profile":
        return _handle_profile(args, chat_id)

    elif cmd == "restart":
        return _handle_restart(chat_id)
    elif cmd == "stop":
        return _handle_stop(chat_id, session_pool, task_queues)
    elif cmd == "backend":
        return _handle_backend(args, chat_id)

    elif cmd == "session":
        return _handle_session(args, chat_id, session_pool, task_queues)

    return False


def _format_uptime(seconds):
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        m, s = divmod(int(seconds), 60)
        return f"{m}m{s}s"
    h, remainder = divmod(int(seconds), 3600)
    m, s = divmod(remainder, 60)
    return f"{h}h{m}m"


def _handle_status(chat_id, session_pool, task_queues):
    from .config import ACTIVE_PROFILE, ACTIVE_BACKEND, BACKENDS, get_app_context
    from .backend import list_backends
    from .queue import get_execution_history
    from .event_consumer import _restart_fail_count

    profile = get_active_profile()
    sessions_info = session_pool.list_sessions()
    ctx = get_app_context()

    lines = []
    lines.append("📊 **状态**")

    if _service_start_time:
        uptime = time.time() - _service_start_time
        lines.append(t("status.running_time", uptime=_format_uptime(uptime)))
    lines.append(t("status.backend", backend=ACTIVE_BACKEND, backends=", ".join(list_backends().keys())))
    lines.append(t("status.profile", profile=ACTIVE_PROFILE, model=profile.get('model', 'N/A')))
    lines.append(t("status.active_session", active_name=session_pool.active_name))

    cli_line = ctx.cli_version_cache or "⚠️ 未检测"
    lines.append(f"**{ACTIVE_BACKEND} CLI:** {cli_line}")

    if _restart_fail_count > 0:
        lines.append(t("status.event_fail", count=_restart_fail_count))
    else:
        lines.append(t("status.event_ok"))

    lines.append("")
    lines.append(f"**{t('status.sessions_header')}**")

    for si in sessions_info:
        marker = " 🟢" if si["is_active"] else ""
        tq = task_queues.get(si["name"])
        if tq:
            qi = tq.status_info
            busy_text = "🟡 执行中" if qi["running"] else "🟢 空闲"
            current = _smart_truncate(qi["current"], 60) if qi["current"] else "-"
            lines.append(t("status.session_line", name=si['name'], marker=marker))
            lines.append(t("status.session_state", busy=busy_text, msg_count=si['message_count'], completed=qi['total_completed']))
            if qi["running"]:
                lines.append(t("status.session_current", current=current))
        else:
            lines.append(t("status.session_no_queue", name=si['name'], marker=marker))

    history = get_execution_history(limit=5)
    if history:
        lines.append("")
        lines.append(f"**{t('status.history_header')}**")
        for h in reversed(history):
            status = "✅" if h["exit_code"] == 0 else "⚠️" if h["exit_code"] == -2 else "❌"
            lines.append(t("status.history_line", status=status, time=h['time'], session=h['session'], content=_smart_truncate(h['content'], 30), duration=h['duration_s']))

    send_feishu_markdown("\n".join(lines), chat_id)
    return True


def _handle_reset(chat_id, session_pool):
    session = session_pool.get_active()
    new_id = session.reset()
    session_pool._persist()
    send_feishu_markdown(
        t("reset.content", name=session.name, new_id=new_id),
        chat_id,
    )
    return True


def _handle_restart(chat_id):
    """Handle /restart command - restart the entire service (Task #12)."""
    send_feishu_markdown(
        t("restart.content"),
        chat_id,
    )

    from .config import get_app_context
    ctx = get_app_context()
    ctx.running = False

    BASE_DIR = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))
    restart_script = os.path.join(BASE_DIR, "restart_service.py")

    current_pid = str(os.getpid())
    subprocess.Popen(
        [sys.executable, restart_script, "--exclude-pid", current_pid],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        cwd=BASE_DIR,
    )

    time.sleep(1)
    os.kill(os.getpid(), signal.SIGTERM)

    return True


def _handle_stop(chat_id, session_pool, task_queues):
    """Handle /stop command with wait-for-confirmation (Task #13)."""
    active_name = session_pool.active_name
    tq = task_queues.get(active_name)

    if not tq or not tq.is_busy:
        send_reply(t("stop.idle_desc", name=active_name), chat_id)
        return True

    from .backend import get_backend_pid
    pid = get_backend_pid()
    current = tq.status_info.get("current", "unknown")
    stopped = tq.stop_current()

    if stopped:
        actually_stopped = False
        for i in range(6):
            time.sleep(0.5)
            if get_backend_pid() is None:
                actually_stopped = True
                break

        if actually_stopped:
            send_feishu_markdown(
                t("stop.done_desc", name=active_name, task=current, pid=pid or "N/A"),
                chat_id,
            )
        else:
            send_feishu_markdown(
                t("stop.fail_desc") + f"\n\nSession: `{active_name}`\n任务: {current}\nPID: `{pid or 'N/A'}` 进程未在3秒内退出，可能需要手动处理",
                chat_id,
            )
    else:
        send_reply(t("stop.fail_desc"), chat_id)

    return True


def _handle_profile(args, chat_id):
    from .config import ACTIVE_PROFILE, get_app_context

    # 执行中禁止切换 (Task #14)
    ctx = get_app_context()
    if ctx.session_pool:
        tq = ctx.task_queues.get(ctx.session_pool.active_name)
        if tq and tq.is_busy:
            send_reply(t("stop.switch_desc"), chat_id)
            return True

    if not args.strip():
        profiles = list_profiles()
        current = ACTIVE_PROFILE
        lines = []
        lines.append("📋 **配置档案**")
        for name, desc in profiles.items():
            marker = " ✅ 当前" if name == current else ""
            lines.append(f"- **{name}**{marker}: {desc}")
        lines.append("")
        lines.append(t("profile.list_hint"))
        send_feishu_markdown("\n".join(lines), chat_id)
        return True

    profile_name = args.strip().lower()
    profile = get_profile(profile_name)
    if not profile:
        send_feishu_markdown(t("profile.not_found_desc", name=profile_name), chat_id)
        return True

    set_active_profile(profile_name)
    overrides = profile.get("config_overrides", [])
    overrides_text = f"- 配置覆盖: {', '.join(overrides)}" if overrides else ""
    content = t("profile.switched_desc", name=profile_name, model=profile.get('model', 'N/A'), desc=profile.get('description', 'N/A'), overrides=overrides_text)
    send_feishu_markdown(content, chat_id)
    return True


def get_profile(name):
    from .config import PROFILES
    return PROFILES.get(name, None)


def _handle_backend(args, chat_id):
    from .config import ACTIVE_BACKEND, BACKENDS, set_active_backend, get_app_context
    from .backend import list_backends

    # 执行中禁止切换 (Task #14)
    ctx = get_app_context()
    if ctx.session_pool:
        tq = ctx.task_queues.get(ctx.session_pool.active_name)
        if tq and tq.is_busy:
            send_reply(t("stop.switch_desc"), chat_id)
            return True

    if not args.strip():
        backends = list_backends()
        current = ACTIVE_BACKEND
        lines = []
        lines.append("🤖 **后端列表**")
        for name, label in backends.items():
            marker = " ✅ 当前" if name == current else ""
            cfg = BACKENDS.get(name, {})
            path = cfg.get("path", "N/A")
            lines.append(f"- **{name}**{marker}: {label} (`{path}`)")
        lines.append("")
        lines.append(t("backend.list_hint"))
        send_feishu_markdown("\n".join(lines), chat_id)
        return True

    backend_name = args.strip().lower()
    if backend_name not in BACKENDS:
        available = ", ".join(BACKENDS.keys())
        send_feishu_markdown(
            t("backend.not_found_desc", name=backend_name, available=available),
            chat_id,
        )
        return True

    if backend_name == ACTIVE_BACKEND:
        send_feishu_markdown(
            t("backend.already_desc", name=backend_name),
            chat_id,
        )
        return True

    ok = set_active_backend(backend_name)
    if ok:
        cfg = BACKENDS.get(backend_name, {})
        send_feishu_markdown(
            t("backend.switched_desc", name=backend_name, path=cfg.get('path', 'N/A'), sid=cfg.get('session_id', 'N/A'), timeout=cfg.get('timeout', 'N/A')),
            chat_id,
        )
    else:
        send_feishu_markdown(t("backend.switch_fail_desc", name=backend_name), chat_id)
    return True


def _handle_session(args, chat_id, session_pool, task_queues):
    from .queue import TaskQueue

    if not args.strip():
        return _session_list(chat_id, session_pool, task_queues)

    parts = args.strip().split(None, 1)
    sub_cmd = parts[0].lower()
    sub_args = parts[1].strip() if len(parts) > 1 else ""

    if sub_cmd == "new":
        return _session_new(sub_args, chat_id, session_pool, task_queues)
    elif sub_cmd == "switch":
        return _session_switch(sub_args, chat_id, session_pool, task_queues)
    elif sub_cmd == "kill":
        return _session_kill(sub_args, chat_id, session_pool, task_queues)
    elif session_pool.get(sub_cmd) is not None:
        return _session_switch(sub_cmd, chat_id, session_pool, task_queues)
    else:
        send_feishu_markdown(
            t("session.unknown_desc") + f"\n\n💡 提示: `/session {sub_cmd}` 不是有效命令。如需切换 Session，请使用 `/session switch {sub_cmd}` 或直接 `/session switch <name>`",
            chat_id,
        )
        return True


def _session_list(chat_id, session_pool, task_queues):
    sessions = session_pool.list_sessions()
    lines = []
    lines.append("📂 **所有 Session**")
    for si in sessions:
        marker = " 🟢 当前" if si["is_active"] else ""
        tq = task_queues.get(si["name"])
        busy = "🟡 执行中" if (tq and tq.is_busy) else "🟢 空闲"
        lines.append(f"- **`{si['name']}`**{marker}: {busy} | 消息: {si['message_count']} | 活跃: {si['last_active_ago']}前")

    lines.append("")
    lines.append(t("session.list_hint"))
    send_feishu_markdown("\n".join(lines), chat_id)
    return True


def _session_new(name, chat_id, session_pool, task_queues):
    from .queue import TaskQueue
    from .config import get_app_context

    ctx = get_app_context()

    if not name:
        send_reply(t("session.create_missing_desc"), chat_id)
        return True

    name = name.lower().strip()
    if not name.replace("-", "").replace("_", "").isalnum():
        send_reply(t("session.create_invalid_desc"), chat_id)
        return True

    session = session_pool.create(name)
    if session is None:
        send_feishu_markdown(t("session.create_dup_desc", name=name), chat_id)
        return True

    tq = TaskQueue(session)
    tq.start_worker()
    with ctx.task_queues_lock:
        task_queues[name] = tq

    send_feishu_markdown(
        t("session.created_desc", name=name, sid=session.session_id),
        chat_id,
    )
    return True


def _session_switch(name, chat_id, session_pool, task_queues):
    if not name:
        send_reply(t("session.switch_missing_desc"), chat_id)
        return True

    name = name.lower().strip()
    session = session_pool.switch(name)
    if session is None:
        send_feishu_markdown(t("session.switch_notfound_desc", name=name), chat_id)
        return True

    tq = task_queues.get(name)
    busy = "🟡 执行中" if (tq and tq.is_busy) else "🟢 空闲"
    send_feishu_markdown(
        t("session.switched_desc", name=name, busy=busy, sid=session.session_id),
        chat_id,
    )
    return True


def _session_kill(name, chat_id, session_pool, task_queues):
    from .config import get_app_context

    ctx = get_app_context()

    if not name:
        send_reply(t("session.kill_missing_desc"), chat_id)
        return True

    name = name.lower().strip()
    if name == "main":
        send_reply(t("session.kill_main_desc"), chat_id)
        return True

    with ctx.task_queues_lock:
        tq = task_queues.get(name)
    if tq and tq.is_busy:
        tq.stop_current()

    with ctx.task_queues_lock:
        task_queues.pop(name, None)

    killed = session_pool.kill(name)
    if killed:
        new_active = session_pool.active_name
        send_feishu_markdown(
            t("session.killed_desc", name=name, new_active=new_active),
            chat_id,
        )
    else:
        send_feishu_markdown(t("session.kill_fail_desc", name=name), chat_id)

    return True
