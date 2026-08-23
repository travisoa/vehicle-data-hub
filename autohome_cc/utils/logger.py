"""运行日志。"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path


class RunLogger:
    """同时写文件日志并累计 Excel logs sheet 所需记录。"""

    def __init__(self, log_dir: Path) -> None:
        self.log_dir = log_dir
        self.log_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.log_path = self.log_dir / f"run_{timestamp}.log"
        self.records: list[dict] = []

        self.logger = logging.getLogger(f"autohome_mvp_{timestamp}")
        self.logger.setLevel(logging.INFO)
        self.logger.handlers.clear()

        file_handler = logging.FileHandler(self.log_path, encoding="utf-8")
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
        )
        self.logger.addHandler(file_handler)

    def info(self, message: str, **kwargs) -> None:
        self._write("INFO", message, **kwargs)

    def warning(self, message: str, **kwargs) -> None:
        self._write("WARNING", message, **kwargs)

    def error(self, message: str, **kwargs) -> None:
        self._write("ERROR", message, **kwargs)

    def _write(self, level: str, message: str, **kwargs) -> None:
        text = self._format_message(message, **kwargs)
        self.logger.log(getattr(logging, level), text)
        self.records.append(
            {
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "level": level,
                "model": kwargs.get("model", ""),
                "stage": kwargs.get("stage", ""),
                "message": message,
                "details": "; ".join(
                    f"{key}={value}"
                    for key, value in kwargs.items()
                    if key not in {"model", "stage"}
                ),
            }
        )

    @staticmethod
    def _format_message(message: str, **kwargs) -> str:
        if not kwargs:
            return message
        suffix = " ".join(f"{key}={value}" for key, value in kwargs.items())
        return f"{message} {suffix}"
