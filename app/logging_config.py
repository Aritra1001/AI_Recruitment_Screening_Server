"""Application logging.

Everything under the "app" logger goes to the console and logs/app.log. Job and resume
activity (app.jobs, app.resumes) is additionally written to its own audit file so there is
one place to check who created, read, updated or deleted what.
"""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app.config import settings

LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_MAX_BYTES = 5 * 1024 * 1024
_BACKUPS = 5


def _file_handler(path: Path, formatter: logging.Formatter) -> RotatingFileHandler:
    handler = RotatingFileHandler(path, maxBytes=_MAX_BYTES, backupCount=_BACKUPS, encoding="utf-8")
    handler.setFormatter(formatter)
    return handler


def setup_logging() -> None:
    app_logger = logging.getLogger("app")
    if app_logger.handlers:  # already configured (e.g. module re-imported by the reloader)
        return

    log_dir = Path(settings.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(LOG_FORMAT)

    console = logging.StreamHandler()
    console.setFormatter(formatter)

    app_logger.setLevel(settings.log_level.upper())
    app_logger.addHandler(console)
    app_logger.addHandler(_file_handler(log_dir / "app.log", formatter))
    for name, filename in (("app.jobs", "jobs_audit.log"), ("app.resumes", "resumes_audit.log")):
        logging.getLogger(name).addHandler(_file_handler(log_dir / filename, formatter))
