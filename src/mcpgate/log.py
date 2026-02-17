"""Logging configuration for the application using Loguru."""

from __future__ import annotations

__all__: tuple[str, ...] = (
    "InterceptHandler",
    "Level",
    "setup_logging",
    "setup_std_logging",
    "setup_warnings",
)

import inspect
import logging
import sys
import warnings
from enum import StrEnum
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    import loguru


class Level(StrEnum):
    """Logging levels."""

    TRACE = "TRACE"
    DEBUG = "DEBUG"
    INFO = "INFO"
    SUCCESS = "SUCCESS"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class InterceptHandler(logging.Handler):
    """Route standard logging records through Loguru."""

    def emit(self, record: logging.LogRecord) -> None:
        """Emit a logging record."""
        # Get corresponding Loguru level if it exists.
        level: str | int
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        # Find caller from where originated the logged message.
        frame, depth = inspect.currentframe(), 0
        while frame and (depth == 0 or frame.f_code.co_filename == logging.__file__):
            frame = frame.f_back
            depth += 1

        logger.opt(depth=depth, exception=record.exc_info).log(
            level, record.getMessage()
        )


def setup_std_logging() -> None:
    """Configure logging to route standard logging through Loguru."""
    logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)


def setup_warnings() -> None:
    """Configure warnings to be logged through Loguru."""
    showwarning_ = warnings.showwarning

    def showwarning(message, *args, **kwargs) -> None:  # type: ignore[no-untyped-def] # noqa: ANN001, ANN002, ANN003
        logger.opt(depth=2).warning(message)  # type: ignore[no-untyped-call]
        showwarning_(message, *args, **kwargs)  # type: ignore[no-untyped-call]

    warnings.showwarning = showwarning


def setup_logging(
    *,
    level: Level = Level.INFO,
    serialize: bool = False,
    logger_levels: loguru.FilterDict | None = None,
) -> None:
    """Setup logging."""
    logger.enable("mcpgate")
    logger.remove()
    logger.add(
        sys.stderr,
        level=level,
        serialize=serialize,
        filter=logger_levels,
    )
    setup_std_logging()
    setup_warnings()
