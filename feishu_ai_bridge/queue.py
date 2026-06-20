import queue
import time
import threading
from datetime import datetime

from .config import CHAT_ID, MAX_QUEUE_SIZE, get_app_context
from .feishu import log, log_warn, log_error, log_debug, send_feishu, send_reply, send_feishu_markdown
from .backend import run_backend, cancel_backend
from .i18n import t

_execution_history = []
_history_lock = threading.Lock()
MAX_HISTORY = 20
MAX_PENDING_MERGE = 3

THINKING_HINT_THRESHOLD = 8

# Simple question keywords for Task #7
SIMPLE_QUESTION_KEYWORDS = ['创建', '写', '修改', '删除', '文件', '代码', 'git']


def get_execution_history(limit=5):
    with _history_lock:
        return list(_execution_history[-limit:])


def _record_execution(session_name, content, exit_code, duration_s, model=""):
    with _history_lock:
        _execution_history.append({
            "session": session_name,
            "content": content[:80],
            "exit_code": exit_code,
            "duration_s": round(duration_s, 1),
            "model": model,
            "time": datetime.now().strftime("%H:%M:%S"),
        })
        while len(_execution_history) > MAX_HISTORY:
            _execution_history.pop(0)


class TaskQueue:
    def __init__(self, session):
        self._session = session
        self._queue = queue.Queue(maxsize=MAX_QUEUE_SIZE)
        self._running = False
        self._current_task = None
        self._lock = threading.Lock()
        self._pending_messages = []
        self._pending_lock = threading.Lock()
        self._total_completed = 0
        self._worker_thread = None
        self._consecutive_failures = 0  # Task #8: worker auto-recovery counter

    def submit(self, content, chat_id, is_command, task_queues=None):
        """Submit a task to the queue. task_queues dict is used for dynamic idle hints (Task #18)."""
        try:
            self._queue.put_nowait({
                "content": content,
                "chat_id": chat_id,
                "is_command": is_command,
                "time": time.time(),
            })
            qsize = self._queue.qsize()
            if qsize > 1:
                send_feishu_markdown(t("queue.queued", name=self._session.name, size=qsize), chat_id)
            return True
        except queue.Full:
            # Task #18: dynamic idle Session hints
            if task_queues:
                idle = [name for name, tq in task_queues.items()
                        if not tq.is_busy and name != self._session.name]
                hint = t("queue.full_idle", idle=", ".join(f"`{n}`" for n in idle)) if idle else t("queue.full_retry")
            else:
                hint = t("queue.full_hint")
            send_feishu_markdown(t("queue.full", name=self._session.name) + hint, chat_id)
            return False

    def inject_pending(self, content):
        with self._pending_lock:
            self._pending_messages.append(content)
        log_debug(f"[{self._session.name}] 消息已注入待处理队列: {content[:50]}")

    def get_pending(self):
        with self._pending_lock:
            msgs = self._pending_messages[:]
            self._pending_messages.clear()
            return msgs

    @property
    def is_busy(self):
        with self._lock:
            return self._running

    @property
    def current_task(self):
        with self._lock:
            return self._current_task

    @property
    def status_info(self):
        """Defensive status access (Task #17)."""
        with self._lock:
            current = self._current_task
            current_text = current.get("content", "")[:80] if isinstance(current, dict) else None
            return {
                "session_name": self._session.name,
                "running": self._running,
                "current": current_text,
                "queue_size": self._queue.qsize(),
                "total_completed": self._total_completed,
            }

    def stop_current(self):
        with self._lock:
            if not self._running:
                return False
            task_desc = self._current_task["content"][:50] if isinstance(self._current_task, dict) else "unknown"
        cancelled = cancel_backend()
        if cancelled:
            log_debug(f"[{self._session.name}] 已发送停止信号: {task_desc}")
        return cancelled

    def start_worker(self):
        self._worker_thread = threading.Thread(target=self._worker, daemon=True)
        self._worker_thread.start()
        log_debug(f"[{self._session.name}] 任务执行线程已启动")

    def _worker(self):
        """Worker loop with auto-recovery on transient failures (Task #8)."""
        while True:
            task_success = False
            try:
                task = self._queue.get()
                with self._lock:
                    self._running = True
                    self._current_task = task
                try:
                    self._execute(task)
                    task_success = True
                    self._consecutive_failures = 0
                except Exception as e:
                    self._consecutive_failures += 1
                    if self._consecutive_failures <= 3:
                        log_warn(f"[{self._session.name}] Worker异常 #{self._consecutive_failures}，自动重启线程")
                        send_feishu_markdown(t("queue.internal_error", name=self._session.name), task.get("chat_id", CHAT_ID))
                        time.sleep(2)
                    else:
                        log_error(f"[{self._session.name}] Worker线程连续失败{self._consecutive_failures}次，停止恢复")
                        send_feishu_markdown(
                            f"🚨 Worker异常\n\n{t('queue.worker_error', name=self._session.name)}",
                            CHAT_ID,
                        )
                        break
                finally:
                    with self._lock:
                        self._running = False
                        self._current_task = None
                        if task_success:
                            self._total_completed += 1
                    self._queue.task_done()
            except Exception as e:
                log_error(f"[{self._session.name}] Worker异常: {e}")
                time.sleep(1)

    def _execute(self, task):
        from .config import TRAECLI_TIMEOUT, SESSION_TIMEOUT_SECONDS

        content = task["content"]
        chat_id = task["chat_id"]
        is_command = task["is_command"]
        original_content = content

        # Task #7: simple question detection — skip unnecessary tool use
        content = _apply_simple_question_hint(content, is_command)

        # Task #11: timeout check BEFORE execution starts
        timed_out = self._session.check_and_consume_timeout()
        if timed_out:
            timeout_h = SESSION_TIMEOUT_SECONDS // 3600
            timeout_m = (SESSION_TIMEOUT_SECONDS % 3600) // 60
            if timeout_h > 0:
                timeout_desc = f"{timeout_h}小时"
                if timeout_m > 0:
                    timeout_desc += f"{timeout_m}分钟"
            else:
                timeout_desc = f"{timeout_m}分钟"
            send_feishu_markdown(
                t("exec.timeout_desc", name=self._session.name, timeout_desc=timeout_desc),
                chat_id,
            )

        # Touch but don't reset the timeout timer here (Task #11: prevent race)
        self._session.touch()
        session_id = self._session.session_id

        pending = self.get_pending()
        if pending:
            to_merge = pending[:MAX_PENDING_MERGE]
            content = content + "\n\n---\n\n" + "\n\n---\n\n".join(to_merge)
            log_debug(f"[{self._session.name}] 合并 {len(to_merge)} 条待处理消息")
            # 超出上限的放回pending队列等待下一轮
            remaining = pending[MAX_PENDING_MERGE:]
            if remaining:
                with self._pending_lock:
                    self._pending_messages[:0] = remaining
                log_debug(f"[{self._session.name}] {len(remaining)} 条消息超出合并上限，保留至下一轮")

        # Brief thinking indicator — 仅对可能耗时的任务显示，简单问答跳过以减少噪音
        show_thinking = is_command or len(original_content) > 50 or any(
            kw in original_content for kw in SIMPLE_QUESTION_KEYWORDS
        )
        if show_thinking:
            send_reply("💭 思考中...", chat_id)

        task_start = time.time()
        output, exit_code, meta = run_backend(content, session_id, chat_id)
        task_duration = time.time() - task_start
        log_debug(f"[{self._session.name}] 后端返回: exit_code={exit_code}, output_len={len(output)}")

        # Task #11: delay the timeout after execution completes
        self._session.update_last_active()

        duration_s = meta.get("duration_ms", 0) / 1000
        num_turns = meta.get("num_turns", 0)
        step_count = meta.get("step_count", 0)
        model = meta.get("model", "")

        _record_execution(self._session.name, task["content"], exit_code, task_duration, model)

        exit_desc = _translate_exit_code(exit_code, output)

        # 构建回复：元信息头部 + AI 输出正文
        # 闲聊（非指令）且快速完成时，精简元信息，只保留状态图标
        is_chat = not is_command
        is_quick = task_duration < THINKING_HINT_THRESHOLD and step_count == 0

        header_lines = []
        if exit_code == 0:
            if is_chat and is_quick:
                pass  # 闲聊快速回复：不加任何头部，直接输出正文
            else:
                header_lines.append("✅ 完成")
        elif exit_code == -2:
            header_lines.append("🛑 已停止")
        else:
            header_lines.append("⚠️ 执行遇到问题")

        # 元信息：仅指令任务或非快速完成时显示
        if not (is_chat and is_quick):
            meta_parts = []
            if model:
                meta_parts.append(f"模型: {model}")
            meta_parts.append(f"耗时: {duration_s:.1f}s")
            if step_count > 0:
                meta_parts.append(f"步骤: {step_count}")
            header_lines.append(" | ".join(meta_parts))

        if exit_desc:
            header_lines.append(exit_desc)

        # 用 Markdown 发送，让表格/标题/代码块在飞书正确渲染
        if header_lines:
            header_md = "\n".join(header_lines)
            final_md = f"{header_md}\n\n---\n\n{output}"
        else:
            final_md = output

        send_feishu_markdown(final_md, chat_id)


def _apply_simple_question_hint(content: str, is_command: bool) -> str:
    """Task #7: Detect simple questions and add a hint to skip unnecessary tool use."""
    if is_command:
        return content
    is_simple = not any(kw in content for kw in SIMPLE_QUESTION_KEYWORDS)
    if is_simple and len(content) < 50 and not content.strip().endswith(("。", ".", "!", "！", "?", "？")):
        return "[Direct Answer Hint: 这是一个简单问答，请直接回答，不需要先检查skills或运行工具]\n" + content
    return content


def _translate_exit_code(exit_code, output=""):
    from .config import TRAECLI_TIMEOUT, ACTIVE_BACKEND, BACKENDS
    if exit_code == 0:
        return ""
    if exit_code == -2:
        return "任务被用户强制停止"
    if exit_code == -1:
        lower = output.lower()
        if "timeout" in lower or "超时" in lower:
            backend_cfg = BACKENDS.get(ACTIVE_BACKEND, {})
            timeout_val = backend_cfg.get("timeout", TRAECLI_TIMEOUT)
            return f"执行超时 (限制{timeout_val // 60}分钟)，请简化指令或拆分任务后重试"
        if "network" in lower or "网络" in lower or "connection" in lower:
            return "网络连接异常，请检查网络后重试"
        if "rate limit" in lower or "限流" in lower:
            return "API 限流，请稍后重试"
        return "执行异常，请查看详细错误信息"

    if exit_code == 1:
        lower = output.lower()
        if "keyring" in lower:
            return "认证服务异常 (keyring 不可用)"
        if "token" in lower or "auth" in lower or "login" in lower:
            return "认证失败，请检查 traecli 登录状态"
        if "session" in lower:
            return "会话异常，请尝试 /reset 重置"
        if "model" in lower:
            return "模型配置错误，请检查 /profile 设置"
        return "命令执行失败 (退出码 1)"

    if exit_code == 2:
        return "参数错误，请检查指令格式"

    return f"未知错误 (退出码: {exit_code})"
