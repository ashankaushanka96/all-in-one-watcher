import configparser
import os
import shutil
from pathlib import Path
from typing import Dict, List, Tuple

from loguru import logger

import app.variables as var
from app.config.components import _build_component, _log_directory, build_components_from_parser
from app.api.models import ComponentConfigEntry

api_logger = logger.bind(comp_name="WatcherAPI")


def _config_file_path() -> Path:
    return Path(os.path.join(var.WATCHER_DIRECTORY, var.CONFIG_PATH))


def _backup_file_path() -> Path:
    path = _config_file_path()
    return path.with_suffix(path.suffix + ".bak")


def has_backup() -> bool:
    return _backup_file_path().exists()


def _backup_current_config() -> None:
    """Snapshots config.ini before a change is written; only the latest snapshot is kept."""
    path = _config_file_path()
    if not path.exists():
        return
    shutil.copy2(path, _backup_file_path())


def _load_parser() -> configparser.ConfigParser:
    path = _config_file_path()
    parser = configparser.ConfigParser()
    read_ok = parser.read(path)
    if not read_ok:
        raise FileNotFoundError(f"Config INI not found or unreadable at {path}")
    return parser


def find_component_by_tag(tag: str) -> Dict:
    """Looks up a component's config.ini entry by tag, to find its runScriptPath/runScript."""
    parser = _load_parser()
    for section_id in parser.sections():
        section = parser[section_id]
        if section.get("tag") == tag:
            run_script_path = section.get("runScriptPath")
            run_script = section.get("runScript") or "run.sh"
            return {
                "section_id": section_id,
                "tag": tag,
                "name": section.get("name", section_id),
                "runScriptPath": run_script_path,
                "runScript": run_script,
                "logDirectory": _log_directory(section.get("logDirectory"), run_script_path, section_id, run_script),
            }
    raise KeyError(f"No component with tag {tag!r} found in config.ini")


def read_raw_components() -> List[Dict]:
    parser = _load_parser()
    entries = []
    for section_id in parser.sections():
        section = parser[section_id]
        port_raw = section.get("port")
        entries.append(
            {
                "section_id": section_id,
                "tag": section.get("tag"),
                "name": section.get("name", section_id),
                "port": int(port_raw) if port_raw and str(port_raw).strip() else None,
                "startTime": section.get("startTime"),
                "endTime": section.get("endTime"),
                "runningDates": section.get("runningDates"),
                "maxUpDays": section.get("maxUpDays"),
                "needToUp": section.get("needToUp"),
                "needToSendMail": section.get("needToSendMail"),
                "runScriptPath": section.get("runScriptPath"),
                "runScript": section.get("runScript"),
                "logDirectory": section.get("logDirectory"),
            }
        )
    return entries


def _entry_to_ini_values(entry: ComponentConfigEntry) -> Dict[str, str]:
    values = {
        "tag": entry.tag,
        "name": entry.name,
        "startTime": entry.startTime,
        "endTime": entry.endTime,
        "runningDates": "[" + ",".join(str(day) for day in entry.runningDates) + "]",
        "maxUpDays": str(entry.maxUpDays),
        "needToUp": "Yes" if entry.needToUp else "No",
        "needToSendMail": "Yes" if entry.needToSendMail else "No",
        "runScriptPath": entry.runScriptPath,
        "runScript": entry.runScript,
    }
    if entry.port is not None:
        values["port"] = str(entry.port)
    if entry.logDirectory:
        values["logDirectory"] = entry.logDirectory
    return values


def _validate_entry(section_id: str, values: Dict[str, str]) -> None:
    """Re-uses the watcher's own config parsing/validation, so a bad add-component request can't write something it'd fail to load."""
    temp_parser = configparser.ConfigParser()
    temp_parser[section_id] = values
    try:
        _build_component(section_id, temp_parser[section_id])
    except Exception as exc:
        raise ValueError(f"Invalid component definition for [{section_id}]: {exc}") from exc


def apply_changes(
    add_entries: List[ComponentConfigEntry],
    remove_section_ids: List[str],
) -> Tuple[List[str], List[str]]:
    path = _config_file_path()
    parser = _load_parser()

    removed: List[str] = []
    for section_id in remove_section_ids:
        if parser.has_section(section_id):
            parser.remove_section(section_id)
            removed.append(section_id)

    added: List[str] = []
    for entry in add_entries:
        if parser.has_section(entry.section_id) or entry.section_id in added:
            raise ValueError(f"Section [{entry.section_id}] already exists in config.ini")
        values = _entry_to_ini_values(entry)
        _validate_entry(entry.section_id, values)
        parser[entry.section_id] = values
        added.append(entry.section_id)

    # Validate the whole resulting file, not just each new section, so a name conflict across sections is caught here too.
    try:
        build_components_from_parser(parser)
    except ValueError as exc:
        raise ValueError(f"Resulting config.ini would be invalid: {exc}") from exc

    _backup_current_config()

    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as config_file:
        parser.write(config_file)
    tmp_path.replace(path)

    api_logger.info(f"config.ini updated: added={added} removed={removed}")
    return added, removed


def rollback_changes() -> Dict[str, List[str]]:
    """Restores config.ini from the single latest pre-change backup, and reports what that undid."""
    path = _config_file_path()
    backup_path = _backup_file_path()
    if not backup_path.exists():
        raise FileNotFoundError("No backup is available to roll back to.")

    current_parser = _load_parser()

    backup_parser = configparser.ConfigParser()
    if not backup_parser.read(backup_path):
        raise FileNotFoundError(f"Backup config not found or unreadable at {backup_path}")

    try:
        build_components_from_parser(backup_parser)
    except ValueError as exc:
        raise ValueError(f"Backup config.ini is invalid: {exc}") from exc

    current_sections = set(current_parser.sections())
    backup_sections = set(backup_parser.sections())

    restored = sorted(backup_sections - current_sections)
    removed = sorted(current_sections - backup_sections)
    changed = sorted(
        section_id
        for section_id in current_sections & backup_sections
        if dict(current_parser[section_id]) != dict(backup_parser[section_id])
    )

    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as config_file:
        backup_parser.write(config_file)
    tmp_path.replace(path)
    backup_path.unlink(missing_ok=True)

    api_logger.info(f"config.ini rolled back: restored={restored} removed={removed} changed={changed}")
    return {"restored": restored, "removed": removed, "changed": changed}
