"""
Logging System

Centralized logging for Jarvis with console and file output.
Supports per-subsystem log level overrides and log rotation.
"""

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional


# Maps logger name prefixes to subsystem keys (used for per-module level overrides)
_SUBSYSTEM_MAP = {
    'core.stt': 'stt',
    'pipeline.stt': 'stt',
    'core.tts': 'tts',
    'pipeline.tts': 'tts',
    'core.llm_router': 'llm',
    'core.llm_server_client': 'llm',
    'jarvis.tool_registry': 'tools',
    'jarvis.tools.': 'tools',
    'jarvis.router': 'router',
    'jarvis.readback': 'router',
    'jarvis.task_planner': 'router',
    'core.skill_manager': 'skills',
    'core.memory_manager': 'memory',
    'core.interaction_cache': 'memory',
    'core.awareness': 'memory',
    'core.self_awareness': 'memory',
    'core.continuous_listener': 'audio',
    'core.vad': 'audio',
    'core.wake_word': 'audio',
    'core.speaker_id': 'audio',
    'core.reminder_manager': 'reminders',
    'core.google_calendar': 'reminders',
    'core.caldav_calendar': 'reminders',
    'core.web_research': 'research',
    'jarvis.mcp_client': 'mcp',
    'jarvis.mcp_server': 'mcp',
    'jarvis.webcam': 'vision',
    'jarvis.tools.capture_webcam': 'vision',
    'desktop_manager': 'desktop',
    'pipeline.coordinator': 'pipeline',
    'core.conversation': 'conversation',
    'core.people_manager': 'conversation',
    'core.user_profile': 'conversation',
    'jarvis.web': 'web',
    'core.news_manager': 'news',
}

# Populated by configure_module_levels() — subsystem key → logging level int
_module_levels = {}


def _resolve_subsystem(name: str) -> Optional[str]:
    """Resolve a logger name to its subsystem key via prefix matching."""
    # Exact match first
    if name in _SUBSYSTEM_MAP:
        return _SUBSYSTEM_MAP[name]
    # Prefix match (for 'jarvis.tools.developer_tools' → 'jarvis.tools.' → 'tools')
    for prefix, subsystem in _SUBSYSTEM_MAP.items():
        if prefix.endswith('.') and name.startswith(prefix):
            return subsystem
    return None


class Logger:
    """Centralized logger for Jarvis"""

    _loggers = {}

    @classmethod
    def get_logger(cls, name: str, config=None) -> logging.Logger:
        """
        Get or create a logger instance

        Args:
            name: Logger name (usually __name__)
            config: Configuration object (optional)

        Returns:
            Configured logger instance
        """
        if name in cls._loggers:
            return cls._loggers[name]

        logger = logging.getLogger(name)

        # Only configure if not already configured
        if not logger.handlers:
            cls._configure_logger(logger, config)

        cls._loggers[name] = logger
        return logger

    @classmethod
    def _configure_logger(cls, logger: logging.Logger, config) -> None:
        """Configure logger with handlers and formatting"""

        # Determine log level
        if config:
            level_str = config.get("logging.level", "INFO")
            log_file = config.get("logging.file")
            console_enabled = config.get("logging.console", True)
            max_size_mb = config.get("logging.max_size_mb", 0)
            backup_count = config.get("logging.backup_count", 3)
        else:
            level_str = "INFO"
            log_file = None
            console_enabled = True
            max_size_mb = 0
            backup_count = 3

        # Override: suppress console logging for non-voice frontends.
        # Use dedicated log files (separate from voice pipeline's jarvis.log)
        # so logs don't get buried in 100K+ voice entries.
        if os.environ.get('JARVIS_LOG_FILE_ONLY'):
            console_enabled = False
            target = os.environ.get('JARVIS_LOG_TARGET', 'console')
            log_file = str(Path(__file__).parent.parent / "logs" / f"{target}.log")

        level = getattr(logging, level_str.upper(), logging.INFO)

        # Per-subsystem override
        subsystem = _resolve_subsystem(logger.name)
        if subsystem and subsystem in _module_levels:
            level = _module_levels[subsystem]

        logger.setLevel(level)

        # Create formatter
        formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )

        # Console handler
        if console_enabled:
            console_handler = logging.StreamHandler(sys.stdout)
            console_handler.setLevel(level)
            console_handler.setFormatter(formatter)
            logger.addHandler(console_handler)

        # File handler (rotating if max_size_mb configured)
        if log_file:
            log_path = Path(log_file)
            log_path.parent.mkdir(parents=True, exist_ok=True)

            if max_size_mb > 0:
                file_handler = RotatingFileHandler(
                    log_file,
                    maxBytes=int(max_size_mb * 1024 * 1024),
                    backupCount=backup_count,
                )
            else:
                file_handler = logging.FileHandler(log_file)
            file_handler.setLevel(level)
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)

        # Prevent propagation to root logger
        logger.propagate = False

    @classmethod
    def configure_module_levels(cls, config) -> None:
        """
        Read per-subsystem log level overrides from config and apply them.

        Call once at startup AFTER all modules have been imported so that
        existing loggers are retroactively updated.

        Config structure:
            logging:
              modules:
                llm: DEBUG
                tools: DEBUG
                stt: WARNING
        """
        modules = config.get("logging.modules", {}) or {}
        if not modules:
            return

        for subsystem, level_str in modules.items():
            if not level_str:
                continue
            level = getattr(logging, str(level_str).upper(), None)
            if level is None:
                continue
            _module_levels[subsystem] = level

        # Retroactively apply to already-created loggers
        for name, logger in cls._loggers.items():
            subsystem = _resolve_subsystem(name)
            if subsystem and subsystem in _module_levels:
                new_level = _module_levels[subsystem]
                logger.setLevel(new_level)
                for handler in logger.handlers:
                    handler.setLevel(new_level)

        # Also apply to loggers created via logging.getLogger() directly
        # (tool_registry, conversation_router, etc.)
        for name, logger in logging.Logger.manager.loggerDict.items():
            if not isinstance(logger, logging.Logger):
                continue
            subsystem = _resolve_subsystem(name)
            if subsystem and subsystem in _module_levels:
                new_level = _module_levels[subsystem]
                logger.setLevel(new_level)
                for handler in logger.handlers:
                    handler.setLevel(new_level)


def get_logger(name: str, config=None) -> logging.Logger:
    """
    Convenience function to get a logger

    Args:
        name: Logger name (usually __name__)
        config: Configuration object (optional)

    Returns:
        Configured logger instance
    """
    return Logger.get_logger(name, config)
