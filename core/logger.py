"""
Logging System

Centralized logging for Jarvis with console and file output.
"""

import logging
import os
import sys
from pathlib import Path
from typing import Optional


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
        else:
            level_str = "INFO"
            log_file = None
            console_enabled = True

        # Override: suppress console logging when running in console mode.
        # Always use a dedicated console log file (separate from voice pipeline's
        # jarvis.log) so console-mode logs don't get buried in 100K+ voice entries.
        if os.environ.get('JARVIS_LOG_FILE_ONLY'):
            console_enabled = False
            log_file = str(Path(__file__).parent.parent / "logs" / "console.log")
        
        level = getattr(logging, level_str.upper(), logging.INFO)
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
        
        # File handler
        if log_file:
            log_path = Path(log_file)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            
            file_handler = logging.FileHandler(log_file)
            file_handler.setLevel(level)
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)
        
        # Prevent propagation to root logger
        logger.propagate = False


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
