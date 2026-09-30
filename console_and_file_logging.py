import logging
import sys
from datetime import datetime

from console_encoding import enable_utf8_console_output

LOG_MESSAGE_FORMAT = "%(asctime)s %(levelname)-7s %(message)s"
LOG_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"
LOG_FILE_NAME_TIMESTAMP_FORMAT = "%Y-%m-%d_%H-%M"


def build_timestamped_log_file_path(logs_directory, log_name):
    return logs_directory / f"{log_name}_{datetime.now().strftime(LOG_FILE_NAME_TIMESTAMP_FORMAT)}.log"


def configure_logging(log_file_path):
    enable_utf8_console_output()
    log_file_path.parent.mkdir(parents=True, exist_ok=True)
    message_formatter = logging.Formatter(LOG_MESSAGE_FORMAT, LOG_TIMESTAMP_FORMAT)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(message_formatter)
    file_handler = logging.FileHandler(log_file_path, encoding="utf-8")
    file_handler.setFormatter(message_formatter)
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    for configured_handler in list(root_logger.handlers):
        root_logger.removeHandler(configured_handler)
    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)
    root_logger.info("Лог цього запуску: %s", log_file_path.resolve())
    return root_logger
