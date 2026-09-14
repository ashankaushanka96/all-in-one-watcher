import os
import socket
import time
from typing import Dict, Set
from threading import Thread, Event
from loguru import logger
from pathlib import Path
from .logging.logger import setup_logger
from .models.component_model import Component
from .config.settings import load_settings
import app.variables as var
from .config.components import load_components, get_max_length_of_component_name
from .watcher.component_watcher import ComponentWatcher
from .services.component_collector import ComponentCollector
from .services.sendmail import get_mail_service
from .services.server_details import ServerDetailsSender, detect_primary_ip

component_collector = ComponentCollector()
server_details_sender = ServerDetailsSender()

threads: Dict[str, Thread] = {}
stop_flags: Dict[str, Event] = {}
service_stop_event = Event()

DEFAULT_LOG_NAME_WIDTH = 6
THREAD_HEALTH_INTERVAL = 60

try:
    var.SERVER_IP = detect_primary_ip()
except Exception:
    var.SERVER_IP = "unknown"

def start_all_watchers(components: Dict[str, Component]):
    started = []
    for component in components.values():
        id = component.id
        name = component.name
        logger_with_name = logger.bind(comp_name=name)
        if id in threads:
            logger_with_name.debug("Watcher already started; skipping.")
            continue
        stop_event = Event()
        stop_flags[id] = stop_event
        watcher = ComponentWatcher(component, stop_event)
        t = Thread(target=watcher.run, args=(), name=f"watcher-{name}", daemon=True)
        t.start()
        threads[id] = t
        started.append(name)
        logger_with_name.debug(
            f"Watcher thread started (tag={component.tag}, port={component.port}, "
            f"needToUp={component.needToUp}, needToSendMail={component.needToSendMail}, "
            f"logDirectory={component.logDirectory})."
        )
    if started:
        logger.bind(comp_name="Main").info(
            f"Started {len(started)} watcher thread(s): {', '.join(sorted(started))}"
        )

def stop_all_watchers(join_timeout: float = 5.0):
    for id, ev in stop_flags.items():
        ev.set()
    for id, t in threads.items():
        try:
            t.join(timeout=join_timeout)
            if t.is_alive():
                logger.bind(comp_name="Main").warning(
                    f"Watcher thread {t.name} did not stop within {join_timeout}s."
                )
        except Exception:
            logger.bind(comp_name="Main").exception(f"Failed to join watcher thread {t.name}.")

def log_dead_watcher_threads(already_reported: Set[str]):
    for component_id, thread in threads.items():
        if thread.is_alive() or component_id in already_reported:
            continue
        already_reported.add(component_id)
        logger.bind(comp_name="Main").error(
            f"Watcher thread {thread.name} has died; component [{component_id}] is no longer being monitored."
        )

def run_component_collector_periodically(components: Dict[str, Component], stop_event: Event):
    interval = load_settings().watcher_settings.component_collection_interval
    collector_logger = logger.bind(comp_name="ComponentCollector")
    collector_logger.info(f"Starting periodic component collection every {interval} seconds.")

    while not stop_event.is_set():
        try:
            component_collector.run(components)
        except Exception:
            collector_logger.exception("Component collection cycle failed; will retry next interval.")
        stop_event.wait(interval)

def start_component_collector(components: Dict[str, Component]) -> Thread:
    collector_thread = Thread(
        target=run_component_collector_periodically,
        args=(components, service_stop_event),
        name="component-collector",
        daemon=True,
    )
    collector_thread.start()
    return collector_thread

def main(args):
    # config_writer resolves the config file via var.CONFIG_PATH, so keep it in sync.
    var.CONFIG_PATH = args.config

    logs_dir = Path(var.WATCHER_DIRECTORY) / "logs"
    debug = getattr(args, "debug", False)
    # Set up before load_components() so a bad config.ini is logged instead of dying silently into run.sh's /dev/null.
    setup_logger(logs_dir, DEFAULT_LOG_NAME_WIDTH, log_filename="watcher.log", debug=debug)
    main_logger = logger.bind(comp_name="Main")
    main_logger.info(
        f"Watcher starting (pid={os.getpid()}, ip={var.SERVER_IP}, dir={var.WATCHER_DIRECTORY}, "
        f"config={args.config}, debug={debug})."
    )

    try:
        components: Dict[str, Component] = load_components(args.config)
    except Exception as exc:
        # The traceback comes from cli.py's catch-all; this only adds the context it can't know.
        main_logger.error(f"Failed to load components from {args.config}; watcher cannot start: {exc}")
        raise

    max_name_length = get_max_length_of_component_name(components)
    setup_logger(logs_dir, max_name_length, log_filename="watcher.log", debug=debug)

    settings = load_settings()
    main_logger.info(
        f"Loaded {len(components)} component(s): {', '.join(sorted(components)) or 'none'}"
    )
    main_logger.info(
        f"watch_interval={settings.watcher_settings.watch_interval}s "
        f"collection_interval={settings.watcher_settings.component_collection_interval}s "
        f"datadog={settings.datadog_metrics.enabled} "
        f"aggregator={settings.aggregator_backend.enabled} "
        f"mail={settings.mail_configs.mail_send}"
    )

    # Auto-registers config.sh's AUTO_ADD_TO_WATCHER=true components before the watch loop starts.
    added = component_collector.auto_register_components(components)
    if added:
        main_logger.warning(
            f"Auto-registered {len(added)} component(s) from config.sh (AUTO_ADD_TO_WATCHER=true): {added}. "
            "Reloading config.ini."
        )
        components = load_components(args.config)
        main_logger.info(f"Reloaded {len(components)} component(s) after auto-registration.")

    server_details_sender.send_details()

    # Loads mail credentials once, up front, so every watcher thread shares the same cached SendMail instance.
    mail_service = get_mail_service()
    if settings.mail_configs.mail_send and not mail_service.mail_enabled:
        main_logger.error("Mail is enabled in config but credentials could not be loaded; alerts will not be sent.")

    if components:
        start_all_watchers(components)
    else:
        logger.bind(comp_name="ComponentCollector").warning("No components found in config; running component collector only.")
    start_component_collector(components)
    main_logger.info("Watcher startup complete; entering supervision loop.")

    reported_dead: Set[str] = set()
    last_health_check = time.monotonic()
    try:
        while True:
            time.sleep(2)
            if time.monotonic() - last_health_check >= THREAD_HEALTH_INTERVAL:
                last_health_check = time.monotonic()
                log_dead_watcher_threads(reported_dead)
    except (KeyboardInterrupt, SystemExit):
        main_logger.info("Shutdown signal received; stopping watchers...")
        service_stop_event.set()
        stop_all_watchers()
        main_logger.info("Watcher stopped.")
    except Exception as e:
        main_logger.error(f"Supervision loop failed: {e}")
        service_stop_event.set()
        stop_all_watchers()
        raise
