import configparser
import os
import re
from datetime import time as dt_time
from typing import Dict, List, Optional
from loguru import logger
from app.models.component_model import Component, ScheduleWindow
from app.utills.helpers import get_instance_id
import app.variables as var

SECONDS_PER_DAY = 24 * 60 * 60
AUTO_LOG_PATH_SENTINEL = "Auto"
AUTO_LOG_PATH_SUBDIRECTORY = "logs"
LOG_PATH_EXPORT_PATTERN = re.compile(r'^\s*export\s+log_path\s*=\s*(.+?)\s*$', re.IGNORECASE)
INSTANCE_ID_VAR_PATTERN = re.compile(r'\$\{?instance_id\}?', re.IGNORECASE)

def _time_to_seconds(effective_day: int, value: dt_time) -> int:
    return (effective_day * SECONDS_PER_DAY) + (value.hour * 3600) + (value.minute * 60) + value.second

def _to_bool(value: Optional[str], default: bool = False) -> bool:
    if value is None:
        return default
    v = str(value).strip().lower()
    if v in {"yes", "true", "1", "y"}:
        return True
    if v in {"no", "false", "0", "n"}:
        return False
    return default

def _to_running_dates(value: Optional[str]) -> List[int]:
    if not value:
        return []
    raw = str(value).strip().strip("[]")
    if not raw:
        return []
    out: List[int] = []
    for part in raw.split(","):
        part = part.strip()
        if part == "":
            continue
        out.append(int(part))
    return out

def _strip_shell_value(raw_value: str) -> str:
    """Strips a trailing shell comment and surrounding quotes from an `export NAME=value` right-hand side."""
    value = raw_value.strip()
    if value[:1] in ('"', "'") and value[-1:] == value[:1] and len(value) >= 2:
        return value[1:-1]
    # Unquoted values can still carry a trailing ` # comment`; quoted ones (handled above) never do.
    return value.split(' #', 1)[0].strip()


def _read_log_path_from_start_script(script_path: str, section_id: str) -> Optional[str]:
    """Extracts the last `export log_path=...` assignment from a component's start script, mirroring bash's last-assignment-wins semantics."""
    try:
        with open(script_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
    except OSError as exc:
        logger.bind(comp_name="Config").error(
            f"Section [{section_id}] sets logDirectory={AUTO_LOG_PATH_SENTINEL} but {script_path} could not be read: {exc}"
        )
        return None

    log_path_value = None
    for line in content.splitlines():
        match = LOG_PATH_EXPORT_PATTERN.match(line)
        if match:
            log_path_value = _strip_shell_value(match.group(1))

    if not log_path_value:
        logger.bind(comp_name="Config").error(
            f"Section [{section_id}] sets logDirectory={AUTO_LOG_PATH_SENTINEL} but no 'export log_path=' line "
            f"was found in {script_path}."
        )
        return None
    return log_path_value


def _resolve_auto_log_path(runScriptPath: Optional[str], runScript: Optional[str], section_id: str) -> Optional[str]:
    """Derives a dynamic-path component's log directory straight from its own runScript (e.g. an ASG member's start.sh writing to a per-instance directory on a shared EFS mount), so a version/component bump baked into that script needs no config.ini change. The app writes into a fixed 'logs' subfolder under that path, so it's appended here rather than configured."""
    if not runScriptPath or not runScript:
        logger.bind(comp_name="Config").error(
            f"Section [{section_id}] sets logDirectory={AUTO_LOG_PATH_SENTINEL} but runScriptPath/runScript are not both set."
        )
        return None

    script_path = os.path.join(runScriptPath.rstrip('/'), runScript)
    if not os.path.isfile(script_path):
        logger.bind(comp_name="Config").error(
            f"Section [{section_id}] sets logDirectory={AUTO_LOG_PATH_SENTINEL} but {script_path} was not found; "
            "log directory monitoring for this component is disabled."
        )
        return None

    log_path_value = _read_log_path_from_start_script(script_path, section_id)
    if not log_path_value:
        return None

    if INSTANCE_ID_VAR_PATTERN.search(log_path_value):
        instance_id = get_instance_id()
        if not instance_id:
            logger.bind(comp_name="Config").error(
                f"Section [{section_id}] resolved log_path {log_path_value!r} from {script_path} but the "
                "EC2 instance-id could not be resolved; log directory monitoring for this component is disabled."
            )
            return None
        log_path_value = INSTANCE_ID_VAR_PATTERN.sub(instance_id, log_path_value)

    return f"{log_path_value.rstrip('/')}/{AUTO_LOG_PATH_SUBDIRECTORY}"


def _log_directory(
    logDirectory: Optional[str],
    runScriptPath: Optional[str],
    section_id: str = "",
    runScript: Optional[str] = None,
) -> Optional[str]:
    if logDirectory == "No":
        return None
    if logDirectory == AUTO_LOG_PATH_SENTINEL:
        return _resolve_auto_log_path(runScriptPath, runScript, section_id)
    if logDirectory:
        return logDirectory.rstrip('/')
    if not runScriptPath:
        return None
    path = runScriptPath.rstrip('/')
    if path.endswith('/bin'):
        base = path[:-len('/bin')]
        logDirectory = f"{base}/logs"
    else:
        logDirectory = f"{path}/logs"
    return logDirectory if logDirectory != "No" else None

def _normalize_shared_value(value):
    if isinstance(value, str):
        return value.strip()
    return value

def _assert_shared_fields_compatible(existing: Component, candidate: Component, section_id: str):
    shared_fields = (
        "tag",
        "port",
        "maxUpDays",
        "needToUp",
        "needToSendMail",
        "runScriptPath",
        "runScript",
        "logDirectory",
        "startCheckInterval",
    )
    mismatches: List[str] = []
    for field_name in shared_fields:
        existing_value = _normalize_shared_value(getattr(existing, field_name))
        candidate_value = _normalize_shared_value(getattr(candidate, field_name))
        if existing_value != candidate_value:
            mismatches.append(
                f"{field_name}: existing={existing_value!r}, section[{section_id}]={candidate_value!r}"
            )
    if mismatches:
        raise ValueError(
            f"Section [{section_id}] conflicts with component [{existing.name}] on shared fields: "
            + "; ".join(mismatches)
        )

def _build_component(section_id: str, section: configparser.SectionProxy) -> Component:
    port_raw = section.get("port")
    start_check_interval_raw = section.get("startCheckInterval")
    start_time = dt_time.fromisoformat(section.get("startTime"))
    end_time = dt_time.fromisoformat(section.get("endTime"))
    running_dates = _to_running_dates(section.get("runningDates"))
    schedules: Dict[int, List[ScheduleWindow]] = {}
    for running_date in running_dates:
        start_time_seconds = _time_to_seconds(running_date, start_time)
        end_time_seconds = _time_to_seconds(running_date, end_time)
        if end_time < start_time:
            end_time_seconds += SECONDS_PER_DAY

        schedules.setdefault(running_date, []).append(
            ScheduleWindow(
                effectiveDay=running_date,
                startTime=start_time,
                startTimeSeconds=start_time_seconds,
                endTime=end_time,
                endTimeSeconds=end_time_seconds,
            )
        )
    return Component(
        id=section_id,
        tag=section.get("tag"),
        name=section.get("name", section_id).title(),
        port=int(port_raw) if port_raw and str(port_raw).strip() else None,
        schedules=schedules,
        maxUpDays=int(section.get("maxUpDays", "1")),
        needToUp=_to_bool(section.get("needToUp"), False),
        needToSendMail=_to_bool(section.get("needToSendMail"), False),
        runScriptPath=section.get("runScriptPath", None),
        runScript=section.get("runScript", None),
        logDirectory=_log_directory(
            section.get("logDirectory", None),
            section.get("runScriptPath", None),
            section_id,
            section.get("runScript", None),
        ),
        startCheckInterval=(
            int(start_check_interval_raw)
            if start_check_interval_raw and str(start_check_interval_raw).strip()
            else None
        ),
    )

def build_components_from_parser(parser: configparser.ConfigParser) -> Dict[str, Component]:
    """Parses an already-loaded ConfigParser into components; exposed separately from load_components() so config_writer can validate a prospective config.ini before writing it."""
    components: Dict[str, Component] = {}
    parse_errors: List[str] = []
    for section_id in parser.sections():
        section = parser[section_id]
        try:
            parsed_component = _build_component(section_id, section)
            component_key = parsed_component.name
            if component_key in components:
                existing_component = components[component_key]
                _assert_shared_fields_compatible(existing_component, parsed_component, section_id)
                for effective_day, schedule_windows in parsed_component.schedules.items():
                    existing_component.schedules.setdefault(effective_day, []).extend(schedule_windows)
            else:
                components[component_key] = parsed_component
        except Exception as e:
            logger.bind(comp_name="Config").error(f"Error parsing component section [{section_id}]: {e}")
            parse_errors.append(f"[{section_id}] {e}")

    if parse_errors:
        raise ValueError("; ".join(parse_errors))
    return components


def load_components(config_path: str) -> Dict[str, Component]:
    config_file_path = os.path.join(var.WATCHER_DIRECTORY, config_path)
    parser = configparser.ConfigParser()
    read_ok = parser.read(config_file_path)
    if not read_ok:
        raise FileNotFoundError(f"Config INI not found or unreadable at {config_file_path}")
    try:
        return build_components_from_parser(parser)
    except ValueError as e:
        raise ValueError(f"Invalid component config entries in {config_file_path}: {e}") from e

def get_max_length_of_component_name(components: Dict[str, Component]) -> int:
    max_length = 6
    for component in components.values():
        name = component.name or component.id
        max_length = max(max_length, len(name))
    return max_length
