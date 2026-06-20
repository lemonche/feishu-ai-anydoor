"""Qwen Code CLI backend adapter (placeholder / extensible)."""

import os
import subprocess
import threading

from .feishu import log

_current_proc = None
_current_proc_lock = threading.Lock()


def get_current_pid():
    with _current_proc_lock:
        if _current_proc and _current_proc.poll() is None:
            return _current_proc.pid
    return None


def cancel_current():
    with _current_proc_lock:
        if _current_proc and _current_proc.poll() is None:
            log(f"强制终止 qwen 进程 (PID: {_current_proc.pid})")
            try:
                _current_proc.terminate()
            except Exception:
                pass
            try:
                _current_proc.kill()
            except Exception:
                pass
            return True
    return False


def _find_qwen_cli():
    """Locate the Qwen Code CLI executable."""
    candidates = [
        "qwen",
        "qwen-code",
        "qwen2",
        os.path.expanduser("~/.local/bin/qwen"),
        os.path.expanduser("~/.local/bin/qwen-code"),
    ]
    for c in candidates:
        try:
            result = subprocess.run([c, "--version"], capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                return c
        except Exception:
            continue
    return None


def run_qwencli_streaming(instruction, session_id=None, chat_id=None):
    """Run an instruction through Qwen Code CLI.

    Currently this is a placeholder adapter.  If Qwen Code is not installed it
    returns a friendly message explaining how to enable it.
    """
    from .config import CHAT_ID, SESSION_ID
    if chat_id is None:
        chat_id = CHAT_ID
    if session_id is None:
        session_id = SESSION_ID

    qwen_cli = _find_qwen_cli()
    if qwen_cli is None:
        log("Qwen Code CLI 未检测到 (tried: qwen, qwen-code, ~/.local/bin/qwen*)")
        return (
            "⚠️ Qwen Code CLI 尚未安装或不在 PATH 中\n\n"
            "当前默认后端仍为 **Trae CLI**。\n"
            "如需使用 Qwen 后端，请安装 Qwen Code CLI 后重试。\n\n"
            "切换回 Trae 后端：发送 `/profile default` 或修改 settings.yaml 中的 active_backend 为 trae。",
            1,
            {"duration_ms": 0, "num_turns": 0, "step_count": 0, "model": "", "tool_calls": []},
        )

    log(f"调用 qwen ({qwen_cli}): {instruction[:80]}...")

    args = [qwen_cli, "-p", instruction]
    if session_id:
        args.extend(["--session-id", session_id])

    try:
        global _current_proc
        proc = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env={**os.environ, "TERM": "dumb"},
        )
        with _current_proc_lock:
            _current_proc = proc

        stdout, stderr = proc.communicate(timeout=300)

        with _current_proc_lock:
            _current_proc = None

        if proc.returncode != 0:
            err = stderr.strip() or "Qwen 执行失败"
            return err[:500], proc.returncode, {
                "duration_ms": 0, "num_turns": 0, "step_count": 0, "model": "", "tool_calls": []
            }

        output = stdout.strip()
        if len(output) > 4000:
            output = output[:4000] + "\n...(输出过长，已截断)"

        return output, 0, {
            "duration_ms": 0, "num_turns": 0, "step_count": 0, "model": "qwen", "tool_calls": []
        }

    except subprocess.TimeoutExpired:
        proc.kill()
        return "Qwen 执行超时", -1, {}
    except Exception as e:
        return f"Qwen 执行异常: {e}", -1, {}
