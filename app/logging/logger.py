from pathlib import Path
import sys
from typing import Optional
from loguru import logger

def setup_logger(
    logs_dir: Path,
    component_name_width: int,
    log_filename: Optional[str] = None,
    debug: bool = False,
    mode: str = "a",
    default_comp_name: str = "Main",
) -> None:
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_file_path = logs_dir / (log_filename or "watcher_{time:YYYYMMDDHHmmss}.log")
    log_level = "DEBUG" if debug else "INFO"
    logger.remove()
    # Without a default, any logger call that forgets .bind(comp_name=...) is silently dropped by the formatter.
    logger.configure(extra={"comp_name": default_comp_name})
    logger.add(
        str(log_file_path),
        rotation="1 day",
        retention="7 days",
        backtrace=True,
        # diagnose=True renders each frame's variable values, which writes the SMTP password into the log
        # on any sendmail traceback. Loguru appends the traceback itself, so {exception} would duplicate it.
        diagnose=False,
        mode=mode,
        format=(
            "{time:YYYY-MM-DD HH:mm:ss.SSS} | "
            "{level: <7} | "
            f"{{extra[comp_name]: <{component_name_width}}} | "
            "{message}"
        ),
        level=log_level,
    )
    logger.add(
        sys.stderr,
        level="DEBUG",
        diagnose=False,
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <7} | {extra[comp_name]} | {message}\n",
    )
