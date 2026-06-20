import logging
import subprocess
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import LARK_CLI, CHAT_ID, LOG_DIR, LOG_MAX_BYTES, LOG_BACKUP_COUNT

_LARK_MAX_RETRIES = 2
_LARK_RETRY_DELAY = 3

_logger: logging.Logger | None = None


class _BridgeFileHandler(logging.Handler):
    """按日期 + 大小双维度轮转的日志 Handler。

    每天自动切换到新的日志文件（bridge_YYYYMMDD.log），
    单个文件超过 LOG_MAX_BYTES 时触发 RotatingFileHandler 按大小轮转，
    保留 LOG_BACKUP_COUNT 个备份。
    """

    def __init__(self, log_dir: Path, max_bytes: int, backup_count: int):
        super().__init__()
        self._log_dir = log_dir
        self._max_bytes = max_bytes
        self._backup_count = backup_count
        self._current_date: str | None = None
        self._rotating_handler: RotatingFileHandler | None = None

    def _ensure_handler(self) -> RotatingFileHandler | None:
        today = datetime.now().strftime("%Y%m%d")
        if today != self._current_date:
            if self._rotating_handler:
                self._rotating_handler.close()
            self._current_date = today
            self._log_dir.mkdir(parents=True, exist_ok=True)
            log_file = self._log_dir / f"bridge_{today}.log"
            self._rotating_handler = RotatingFileHandler(
                str(log_file),
                maxBytes=self._max_bytes,
                backupCount=self._backup_count,
                encoding="utf-8",
            )
        return self._rotating_handler

    def emit(self, record: logging.LogRecord) -> None:
        try:
            handler = self._ensure_handler()
            if handler:
                handler.emit(record)
        except Exception:
            self.handleError(record)

    def close(self) -> None:
        if self._rotating_handler:
            self._rotating_handler.close()
        super().close()


def _setup_logger() -> logging.Logger:
    global _logger
    if _logger is not None:
        return _logger

    _logger = logging.getLogger("feishu-bridge")
    _logger.setLevel(logging.DEBUG)

    # 文件 handler：按日期 + 大小双维度轮转
    file_handler = _BridgeFileHandler(LOG_DIR, LOG_MAX_BYTES, LOG_BACKUP_COUNT)
    file_handler.setLevel(logging.DEBUG)
    _logger.addHandler(file_handler)

    # 禁止日志向上传播到 root logger
    _logger.propagate = False

    return _logger


def _write_log(level: str, msg: str) -> None:
    """Internal: write a line to rotating log file with timestamp."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] [{level}] {msg}" if level else f"[{ts}] {msg}"
    logger = _setup_logger()
    if level == "DEBUG":
        logger.debug(line)
    elif level == "WARN":
        logger.warning(line)
    elif level == "ERROR":
        logger.error(line)
    else:
        logger.info(line)


# ── Structured logging (Task #3) ──────────────────────────────────


def log(msg: str) -> None:
    """同时输出到控制台和日志文件（带时间戳）。保持向后兼容。"""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    _setup_logger().info(line)


def log_info(msg: str) -> None:
    """控制台 + 文件 INFO 级别日志。等同 log()。"""
    log(msg)


def log_warn(msg: str) -> None:
    """控制台 + 文件，加 [WARN] 前缀。"""
    _write_log("WARN", msg)


def log_error(msg: str) -> None:
    """控制台 + 文件，加 [ERROR] 前缀。"""
    _write_log("ERROR", msg)


def log_debug(msg: str) -> None:
    """仅文件（DEBUG 级别），不输出控制台。"""
    _setup_logger().debug(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [DEBUG] {msg}")


# ── Helpers ───────────────────────────────────────────────────────


def _smart_truncate(text, max_chars):
    """智能截断：尝试在句子边界截断"""
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    if max_chars <= 1:
        return "…"
    truncated = text[:max_chars]
    for sep in ['\n\n', '\n', '。', '！', '？', '.', '!', '?', ' ']:
        idx = truncated.rfind(sep)
        if idx > max_chars * 0.6:
            return truncated[:idx + 1] + "…"
    return truncated[:max_chars - 1] + "…"


def _is_network_error(text):
    lower = text.lower()
    return any(kw in lower for kw in ("timeout", "network", "econnrefused", "connection reset"))


def _run_lark(args, max_retries=_LARK_MAX_RETRIES, timeout=10):
    for attempt in range(max_retries + 1):
        try:
            result = subprocess.run(
                args, capture_output=True, text=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            if attempt < max_retries:
                log_warn(f"飞书API超时，{_LARK_RETRY_DELAY}s后重试 ({attempt+1}/{max_retries})")
                time.sleep(_LARK_RETRY_DELAY)
                continue
            raise
        if result.returncode != 0:
            err = (result.stderr or "")[:200]
            if _is_network_error(err) and attempt < max_retries:
                log_warn(f"飞书API网络错误，{_LARK_RETRY_DELAY}s后重试 ({attempt+1}/{max_retries})")
                time.sleep(_LARK_RETRY_DELAY)
                continue
        return result
    return result


# ── Core send functions ───────────────────────────────────────────


def send_feishu(text, chat_id=CHAT_ID) -> bool:
    """Send plain text via Feishu bot. Returns True on success, False on failure."""
    try:
        result = _run_lark(
            [LARK_CLI, "im", "+messages-send",
             "--as", "bot",
             "--chat-id", chat_id,
             "--text", text],
        )
        if result.returncode == 0:
            log_info("消息已发送到飞书(bot)")
            return True
        else:
            log_warn(f"发送失败: {result.stderr[:200]}")
            return False
    except Exception as e:
        log_error(f"发送异常: {e}")
        return False


def send_reply(text, chat_id=CHAT_ID) -> bool:
    """发送纯文本对话回复，无卡片包装。

    自动截断超过 4000 字符的消息（飞书文本消息上限）。
    """
    if len(text) > 4000:
        text = text[:4000] + "\n…"
    return send_feishu(text, chat_id)


def send_feishu_markdown(md_text, chat_id=CHAT_ID):
    """Send markdown-formatted message via Feishu bot. Returns True on success, False on failure."""
    if len(md_text) > 4000:
        md_text = md_text[:4000 - 20] + "\n…(已截断)"
    try:
        result = _run_lark(
            [LARK_CLI, "im", "+messages-send",
             "--as", "bot",
             "--chat-id", chat_id,
             "--markdown", md_text],
        )
        if result.returncode == 0:
            log_info("Markdown消息已发送到飞书(bot)")
            return True
        else:
            log_warn(f"发送失败: {result.stderr[:200]}")
            return False
    except Exception as e:
        log_error(f"发送异常: {e}")
        return False
