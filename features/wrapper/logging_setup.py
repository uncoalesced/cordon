# Engineered by uncoalesced

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any

LOGGER_NAME = "cordon"
_FORMAT = "%(asctime)s %(levelname)-7s %(name)s %(message)s"

_configured = False


def configure(
    log_path: Path | None = None,
    level: int = logging.INFO,
    to_stderr: bool = True,
) -> logging.Logger:
    global _configured

    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level)
    logger.propagate = False

    if _configured:
        return logger

    formatter = logging.Formatter(_FORMAT)

    if to_stderr:
        stream_handler = logging.StreamHandler(sys.stderr)
        stream_handler.setFormatter(formatter)
        logger.addHandler(stream_handler)

    if log_path is not None:
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.FileHandler(log_path, encoding="utf-8")
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)
        except OSError:
            logger.exception("could not open log file | path=%s", log_path)

    _configured = True
    return logger


def get_logger(suffix: str | None = None) -> logging.Logger:
    if not _configured:
        configure()
    return logging.getLogger(LOGGER_NAME if suffix is None else f"{LOGGER_NAME}.{suffix}")


def log_failure(logger: logging.Logger, message: str, **context: Any) -> None:
    try:
        rendered = json.dumps(context, default=repr, sort_keys=True)
    except (TypeError, ValueError):
        rendered = repr(context)
    logger.error("%s | context=%s", message, rendered, exc_info=True)


def reset_for_tests() -> None:
    global _configured
    logger = logging.getLogger(LOGGER_NAME)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    _configured = False
