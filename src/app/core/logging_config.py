"""Application-wide logging built on structlog.

All records — the application's own, uvicorn's and SQLAlchemy's — are funnelled
through a single ProcessorFormatter and written simultaneously to stdout and to
a rotating file (see docs/adr/0001-structlog-unified-logging.md).
"""

import logging
import logging.handlers
import sys

import structlog
from structlog.typing import Processor

from app.core.config import Logging

_FILE_MAX_BYTES = 10 * 1024 * 1024
_FILE_BACKUP_COUNT = 5

_SHARED_PROCESSORS: tuple[Processor, ...] = (
    structlog.contextvars.merge_contextvars,
    structlog.stdlib.add_log_level,
    structlog.stdlib.add_logger_name,
    structlog.processors.TimeStamper(fmt="iso", utc=True),
    structlog.processors.StackInfoRenderer(),
)


def _shared_processors(format_name: str) -> list[Processor]:
    """Processors shared by structlog's own pipeline and the formatter pre-chain."""
    processors = list(_SHARED_PROCESSORS)
    if format_name == "json":
        # JSONRenderer cannot serialize raw tracebacks, so stringify them first;
        # ConsoleRenderer renders exceptions itself and looks better unstringified.
        processors.append(structlog.processors.format_exc_info)
    return processors


def _renderer(format_name: str, *, colors: bool) -> Processor:
    """Return the terminal processor for the requested output format."""
    if format_name == "json":
        return structlog.processors.JSONRenderer()
    return structlog.dev.ConsoleRenderer(colors=colors)


def _make_formatter(
    format_name: str, *, colors: bool
) -> structlog.stdlib.ProcessorFormatter:
    """Build the shared formatter for one output channel."""
    return structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=_shared_processors(format_name),
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            _renderer(format_name, colors=colors),
        ],
    )


def configure_logging(settings: Logging) -> None:
    """(Re)configure root, uvicorn and structlog loggers. Safe to call repeatedly."""
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(_make_formatter(settings.format, colors=True))
    handlers: list[logging.Handler] = [stdout_handler]

    if settings.file_enabled:
        settings.file_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            settings.file_path,
            maxBytes=_FILE_MAX_BYTES,
            backupCount=_FILE_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setFormatter(_make_formatter(settings.format, colors=False))
        handlers.append(file_handler)

    root = logging.getLogger()
    root.handlers.clear()
    for handler in handlers:
        root.addHandler(handler)
    root.setLevel(settings.level)

    # Route uvicorn's own records through root so nothing bypasses our formatters.
    # Propagation checks only the emitting logger's level, so align it explicitly.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.setLevel(settings.level)
        uvicorn_logger.propagate = True

    structlog.configure(
        processors=[
            *_shared_processors(settings.format),
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
