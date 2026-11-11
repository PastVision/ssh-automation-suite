"""Application logging.

A single handler writes to the console and to ``logs/app.log`` so that a
misconfigured stdout does not lose startup diagnostics.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from app.core.config import Settings

LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
_CONFIGURED = False


def configure_logging(settings: Settings) -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return

    settings.ensure_directories()
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    formatter = logging.Formatter(LOG_FORMAT)

    console = logging.StreamHandler()
    console.setFormatter(formatter)

    file_handler = RotatingFileHandler(
        settings.log_dir / "app.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(console)
    root.addHandler(file_handler)

    for noisy in ("apscheduler.executors.default", "apscheduler.scheduler"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _CONFIGURED = True
