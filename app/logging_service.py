from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler

from flask import Flask

from .settings import SettingKeys, settings


def configure_logging(app: Flask) -> None:
    level_name = settings.get(SettingKeys.LOG_LEVEL).upper()
    level = getattr(logging, level_name, logging.INFO)
    app.logger.setLevel(level)
    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    for handler in list(root_logger.handlers):
        root_logger.removeHandler(handler)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    handler_name = settings.get(SettingKeys.LOG_HANDLER).lower()
    if handler_name == "graylog":
        handler = _build_graylog_handler(formatter)
    else:
        handler = _build_file_handler(formatter)
    root_logger.addHandler(handler)
    app.logger.handlers = [handler]


def _build_file_handler(formatter: logging.Formatter) -> logging.Handler:
    file_path = settings.get(SettingKeys.LOG_FILE_PATH)
    directory = os.path.dirname(file_path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    handler = RotatingFileHandler(file_path, maxBytes=10_000_000, backupCount=10, encoding="utf-8")
    handler.setFormatter(formatter)
    return handler


def _build_graylog_handler(formatter: logging.Formatter) -> logging.Handler:
    import graypy

    host = settings.get(SettingKeys.GRAYLOG_HOST)
    port = settings.get(SettingKeys.GRAYLOG_PORT)
    handler = graypy.GELFUDPHandler(host, port)
    handler.setFormatter(formatter)
    return handler