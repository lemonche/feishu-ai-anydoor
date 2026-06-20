import json
import os
import queue as std_queue
import subprocess
import threading
import time

from .feishu import log, log_info, log_warn, log_error, log_debug, send_reply, send_feishu_markdown

_current_proc = None
_current_proc_lock = threading.Lock()
_cancelled = False

_local = threading.local()

MAX_RETRIES = 2
RETRY_DELAYS = [5, 15]
HEARTBEAT_INTERVAL = 60


def get_current_pid():
    with _current_proc_lock:
        if _current_proc and _current_proc.poll() is None:
            return _current_proc.pid
    return None


def cancel_current():
    global _cancelled
    with _current_proc_lock:
        _cancelled = True
        if _current_proc and _current_proc.poll() is None:
            log_warn(f"强制终止 traecli 进程 (PID: {_current_proc.pid})")
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


def _build_traecli_args(instruction, session_id=None):
    from .config import (
        TRAECLI, TRAECLI_YOLO, TRAECLI_CONFIG_OVERRIDES,
        get_active_profile,
    )
    if session_id is None:
        from .config import SESSION_ID as session_id

    profile = get_active_profile()
    model = profile.get("model", "")
    system_prompt = profile.get("system_prompt", "")
    profile_overrides = profile.get("config_overrides", [])

    effective_instruction = instruction
    if system_prompt and system_prompt.strip():
        effective_instruction = f"[System Instruction]\n{system_prompt.strip()}\n[End System Instruction]\n\n{instruction}"

    args = [TRAECLI, "-p", effective_instruction, "--session-id", session_id]

    if TRAECLI_YOLO:
        args.append("--yolo")

    args.extend(["--output-format", "stream-json", "--include-partial-messages"])

    if model:
        args.extend(["-c", f"model.name={model}"])

    for override in TRAECLI_CONFIG_OVERRIDES:
        args.extend(["-c", override])

    for override in profile_overrides:
        args.extend(["-c", override])

    return args


def _is_non_retryable(exit_code, output):
    lower = output.lower()
    if "keyring is not supported" in lower:
        return True
    if "failed to exchange" in lower and "token" in lower:
        return True
    if exit_code == 1:
        for kw in ("auth", "login", "token", "config", "model", "credential"):
            if kw in lower:
                return True
    return False


def _is_retryable(exit_code, output):
    if exit_code == -1:
        return True
    lower = output.lower()
    for kw in ("timeout", "超时", "network", "网络", "rate limit", "限流", "connection", "econnrefused"):
        if kw in lower:
            return True
    return False


def run_traecli_streaming(instruction, session_id=None, chat_id=None):
    from .config import CHAT_ID, SESSION_ID
    if chat_id is None:
        chat_id = CHAT_ID
    if session_id is None:
        session_id = SESSION_ID

    _local.heartbeat_sent = False

    for attempt in range(MAX_RETRIES + 1):
        output, exit_code, meta = _run_once(instruction, session_id, chat_id)

        if exit_code == 0 or exit_code == -2:
            return output, exit_code, meta

        if _is_non_retryable(exit_code, output):
            log_warn(f"不可重试错误 (exit: {exit_code})，跳过重试")
            return output, exit_code, meta

        if attempt < MAX_RETRIES and _is_retryable(exit_code, output):
            delay = RETRY_DELAYS[attempt]
            log_warn(f"执行失败 (exit: {exit_code})，{delay}s 后重试 ({attempt + 1}/{MAX_RETRIES})...")
            send_feishu_markdown(
                f"🔄 重试中\n\n执行失败，{delay}s 后自动重试\n\n**原因:** {output[:100]}",
                chat_id,
            )
            time.sleep(delay)
            continue

        return output, exit_code, meta

    return output, exit_code, meta


def _run_once(instruction, session_id, chat_id):
    from .config import TRAECLI, TRAECLI_TIMEOUT, STATUS_UPDATE_INTERVAL

    # 进度推送间隔：取配置值与 30s 的较大者，避免频繁刷屏
    progress_interval = max(STATUS_UPDATE_INTERVAL, 30)

    args = _build_traecli_args(instruction, session_id)
    log_debug(f"调用 traecli (streaming): {instruction[:80]}...")
    log_debug(f"  命令: {' '.join(args[:8])}...")
    try:
        global _current_proc, _cancelled
        _cancelled = False
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

        stderr_lines = []

        def _drain_stderr():
            try:
                for line in proc.stderr:
                    stderr_lines.append(line.rstrip())
            except Exception:
                pass

        stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
        stderr_thread.start()

        line_queue = std_queue.Queue()

        def _stdout_reader():
            try:
                for line in proc.stdout:
                    line_queue.put(line)
            except Exception:
                pass
            finally:
                line_queue.put(None)

        reader_thread = threading.Thread(target=_stdout_reader, daemon=True)
        reader_thread.start()

        final_text = ""
        last_progress_time = time.time()
        step_count = 0
        status_lines = []
        tool_calls = []
        partial_text = ""
        duration_ms = 0
        num_turns = 0
        model_name = ""
        task_start_time = time.time()
        instruction_brief = instruction[:60] + ("..." if len(instruction) > 60 else "")

        while True:
            try:
                line = line_queue.get(timeout=HEARTBEAT_INTERVAL)
            except std_queue.Empty:
                if proc.poll() is not None:
                    break
                elapsed_total = time.time() - task_start_time
                _send_heartbeat(step_count, chat_id, elapsed_total, instruction_brief)
                last_progress_time = time.time()
                continue

            if line is None:
                break

            if _cancelled:
                log_info("检测到取消信号，停止读取")
                send_feishu_markdown(f"🛑 已取消\n\n**指令:** {instruction_brief}\n\n任务已被用户停止", chat_id)
                break

            line = line.strip()
            if not line:
                continue

            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            event_type = event.get("type", "")
            subtype = event.get("subtype", "")

            if event_type == "system" and subtype == "status":
                updates = event.get("updates", {})
                title = updates.get("title", "")
                mn = updates.get("model_name", "")
                if mn:
                    model_name = mn
                if title:
                    log_debug(f"  [title] {title}")

            elif event_type == "assistant":
                msg = event.get("message", {})
                tcs = msg.get("tool_calls", [])
                if tcs:
                    for tc in tcs:
                        fn = tc.get("function", {})
                        name = fn.get("name", "")
                        args_str = fn.get("arguments", "")
                        step_count += 1
                        desc = _describe_tool_call(name, args_str)
                        tool_calls.append(desc)
                        status_lines.append(desc)
                        log_debug(f"  [step {step_count}] {desc}")

                        now = time.time()
                        if now - last_progress_time >= progress_interval:
                            _send_progress(step_count, status_lines, chat_id, instruction_brief)
                            last_progress_time = now

                content = msg.get("content", "")
                if content:
                    final_text = content

            elif event_type == "stream_event":
                delta = event.get("delta", {})
                content = delta.get("content", "")
                if content:
                    partial_text += content
                tcs = delta.get("tool_calls", [])
                if tcs:
                    for tc in tcs:
                        fn = tc.get("function", {})
                        name = fn.get("name", "")
                        if name and not any(name in s for s in tool_calls):
                            step_count += 1
                            desc = f"🔧 {name}"
                            tool_calls.append(desc)
                            status_lines.append(desc)
                            log_debug(f"  [step {step_count}] {desc}")

                            now = time.time()
                            if now - last_progress_time >= progress_interval:
                                _send_progress(step_count, status_lines, chat_id, instruction_brief)
                                last_progress_time = now

            elif event_type == "result":
                result_text = event.get("result", "")
                if result_text and not final_text:
                    final_text = result_text
                duration_ms = event.get("duration_ms", 0)
                num_turns = event.get("num_turns", 0)
                is_error = event.get("is_error", False)
                log_debug(f"  [完成] 耗时: {duration_ms}ms, 轮次: {num_turns}, 错误: {is_error}")

        proc.wait(timeout=30)
        stderr_thread.join(timeout=5)
        reader_thread.join(timeout=5)

        with _current_proc_lock:
            _current_proc = None

        if _cancelled:
            return "任务已被用户强制停止", -2, {}

        if proc.returncode != 0 and not final_text and not partial_text:
            stderr_text = "\n".join(stderr_lines)
            error_msg = stderr_text.strip() if stderr_text else f"进程异常退出 (code: {proc.returncode})"
            log_error(f"  [错误] {error_msg[:500]}")
            return error_msg[:500], proc.returncode, {
                "duration_ms": duration_ms, "num_turns": num_turns,
                "step_count": step_count, "model": model_name,
                "tool_calls": tool_calls,
            }

        if not final_text and partial_text:
            final_text = partial_text

        if len(final_text) > 4000:
            original_len = len(final_text)
            final_text = final_text[:4000] + f"\n...(输出过长，已截断 {original_len - 4000} 字符，共 {original_len} 字)"

        meta = {
            "duration_ms": duration_ms,
            "num_turns": num_turns,
            "step_count": step_count,
            "model": model_name,
            "tool_calls": tool_calls,
        }
        return final_text, proc.returncode, meta

    except subprocess.TimeoutExpired:
        proc.kill()
        timeout_min = TRAECLI_TIMEOUT // 60
        return f"执行超时（{timeout_min}分钟限制）", -1, {}
    except Exception as e:
        return f"执行异常: {e}", -1, {}


def _describe_tool_call(name, args_str):
    desc = f"🔧 {name}"
    try:
        args = json.loads(args_str) if isinstance(args_str, str) else args_str
        if "file_path" in args:
            desc += f" → {args['file_path']}"
        elif "command" in args:
            desc += f": {args['command'][:60]}"
        elif "content" in args:
            c = str(args["content"])[:40]
            desc += f" ({c})"
    except (json.JSONDecodeError, TypeError):
        if args_str:
            desc += f": {args_str[:60]}"
    return desc


def _send_progress(step_count, status_lines, chat_id, instruction_brief=""):
    """Send a brief progress update as Markdown."""
    if not status_lines:
        return
    recent = status_lines[-3:]
    lines = [f"⏳ 执行中... (步骤: {step_count})"]
    for s in recent:
        lines.append(f"  {s}")
    send_feishu_markdown("\n".join(lines), chat_id)


def _send_heartbeat(step_count, chat_id, elapsed_total, instruction_brief=""):
    """Send a brief heartbeat as Markdown."""
    elapsed_str = f"{elapsed_total:.0f}s"
    if elapsed_total >= 60:
        m, s = divmod(int(elapsed_total), 60)
        elapsed_str = f"{m}m{s}s"

    send_feishu_markdown(
        f"💭 仍在执行中... (步骤: {step_count}, 已用时: {elapsed_str})",
        chat_id,
    )
