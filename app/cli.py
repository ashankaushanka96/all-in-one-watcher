import argparse
import os
import sys
from pathlib import Path

from loguru import logger

import app.variables as var

API_LOGGER_NAME = "WatcherAPI"


def init_directories() -> None:
    if getattr(sys, "frozen", False):
        base_dir = Path(sys.executable).resolve().parent
    else:
        base_dir = Path(__file__).resolve().parent.parent
    var.WATCHER_DIRECTORY = str(base_dir)
    var.APPS_DIRECTORY = str(base_dir.parent)


def _run_watcher(args: argparse.Namespace) -> None:
    from app.main import main
    try:
        main(args)
    except Exception:
        logger.bind(comp_name="Main").exception("Watcher exited with an unhandled error.")
        raise


def _run_api(args: argparse.Namespace) -> None:
    from app.api.server import run_watcher_api
    from app.config.settings import load_settings
    from app.logging.logger import setup_logger

    var.CONFIG_PATH = args.config

    setup_logger(
        Path(var.WATCHER_DIRECTORY) / "logs",
        len(API_LOGGER_NAME),
        log_filename="api.log",
        mode="a",
        debug=args.debug,
        default_comp_name=API_LOGGER_NAME,
    )
    api_logger = logger.bind(comp_name=API_LOGGER_NAME)
    api_logger.info(f"Watcher API process starting (pid={os.getpid()}, config={args.config}, debug={args.debug}).")
    try:
        run_watcher_api(load_settings())
    except Exception:
        api_logger.exception("Watcher API exited with an unhandled error.")
        raise
    api_logger.info("Watcher API process stopped.")


def _run_monitor(args: argparse.Namespace) -> None:
    from threading import Event
    from app.logging.logger import setup_logger
    from app.services.watcher_monitor import WatcherMonitor, MONITOR_LOGGER_NAME

    setup_logger(
        Path(var.WATCHER_DIRECTORY) / "logs",
        len(MONITOR_LOGGER_NAME),
        log_filename="monitor.log",
        debug=args.debug,
        default_comp_name=MONITOR_LOGGER_NAME,
    )
    monitor_logger = logger.bind(comp_name=MONITOR_LOGGER_NAME)
    monitor_logger.info(f"Monitor process starting (pid={os.getpid()}, debug={args.debug}).")

    stop_event = Event()
    try:
        monitor = WatcherMonitor()
        monitor.run_forever(stop_event)
    except (KeyboardInterrupt, SystemExit):
        stop_event.set()
        monitor_logger.info("Monitor process stopped by signal.")
    except Exception:
        monitor_logger.exception("Monitor exited with an unhandled error.")
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="all_in_one_watcher", description="All-in-one-watcher")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("watcher", help="Run the component watcher")
    run_parser.add_argument("--config", required=True, help="Path to the config file")
    run_parser.add_argument("--debug", action="store_true", help="Enable debug-level logging")
    run_parser.set_defaults(func=_run_watcher)

    api_parser = subparsers.add_parser("api", help="Run the watcher control API")
    api_parser.add_argument("--config", default="./config/config.ini", help="Path to the watcher's config file")
    api_parser.add_argument("--debug", action="store_true", help="Enable debug-level logging")
    api_parser.set_defaults(func=_run_api)

    monitor_parser = subparsers.add_parser("monitor", help="Run the watcher health-check monitor")
    monitor_parser.add_argument("--debug", action="store_true", help="Enable debug-level logging")
    monitor_parser.set_defaults(func=_run_monitor)

    return parser


def main(argv=None) -> None:
    if sys.version_info < (3, 6):
        raise SystemExit("Python 3.6+ is required.")

    init_directories()

    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)
