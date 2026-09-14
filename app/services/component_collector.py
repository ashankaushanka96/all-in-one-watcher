#!/usr/bin/env python3
import os
import re
import io
import datetime
import time
import json
import subprocess
from typing import Dict, List, Optional

import requests
import yaml
from loguru import logger

import app.variables as var
from app.api import config_writer
from app.api.models import ComponentConfigEntry
from app.config.constants import SUBPROCESS_TIMEOUT
from app.config.settings import load_settings
from app.models.aggregator_models import Component, ComponentCollectorRequest, ConfigMeta, ScheduleMeta
from app.models.component_model import Component as WatcherComponent

settings = load_settings()


class ComponentCollector:
    def __init__(self):
        self.base_dir = os.path.abspath(os.path.join(var.WATCHER_DIRECTORY, ".."))
        self.max_depth = 3
        self.exclude_keywords = ['script', 'watcher', 'tar', 'runtime', 'backup', 'jdk', 'dd-agent']
        self.tool_component_names = [n.lower() for n in settings.component_classification.tool_names]
        self.tool_component_prefixes = [p.lower() for p in settings.component_classification.tool_prefixes]
        self.tool_component_suffixes = [s.lower() for s in settings.component_classification.tool_suffixes]
        self.file_extensions = {'.jar': 'Java'}
        self.so_pattern = re.compile(r"\.so(\.?\d+)*$")
        self.status = 'failure'
        self.components = []
        self.message = ''
        self.logger = logger.bind(comp_name="ComponentCollector")
        self.region = settings.meta_data.region
        self.component_db_ingest_url = settings.aggregator_backend.base_url.rstrip("/") + settings.aggregator_backend.component_db_ingest_path
        self.send_component_details = settings.aggregator_backend.enabled

        self.logger.debug(f"Aggregator URL: {self.component_db_ingest_url}")
        self.logger.debug(f"Send Components: {self.send_component_details}")
        self.logger.debug(f"Region: {self.region}")

    def normalize_component_path(self, path: Optional[str]) -> str:
        if not path:
            return ""

        normalized = os.path.normpath(path)

        if os.path.basename(normalized).lower() == "bin":
            normalized = os.path.dirname(normalized)

        return normalized

    def format_component_name(self, name: Optional[str]) -> str:
        return str(name or "").title()

    def normalize_category(self, category: Optional[str]) -> str:
        valid_categories = {'component', 'tool', 'job'}
        normalized = str(category or "").strip().lower()
        return normalized if normalized in valid_categories else 'component'

    def is_tool_component_name(self, name: Optional[str]) -> bool:
        normalized = str(name or "").lower()
        if normalized in self.tool_component_names:
            return True
        if any(normalized.startswith(prefix) for prefix in self.tool_component_prefixes):
            return True
        return any(normalized.endswith(suffix) for suffix in self.tool_component_suffixes)

    def get_day_names(self, running_dates) -> List[str]:
        days_map = {
            0: 'Sunday',
            1: 'Monday',
            2: 'Tuesday',
            3: 'Wednesday',
            4: 'Thursday',
            5: 'Friday',
            6: 'Saturday'
        }
        return [days_map.get(day) for day in running_dates if day in days_map]

    def get_watcher_schedules(self, watcher_component: WatcherComponent) -> List[ScheduleMeta]:
        schedules = [
            schedule
            for effective_day in sorted(watcher_component.schedules)
            for schedule in watcher_component.schedules[effective_day]
        ]
        return [
            ScheduleMeta(
                effective_day=self.get_day_names([schedule.effectiveDay])[0],
                start_time=schedule.startTime.strftime("%H:%M:%S"),
                end_time=schedule.endTime.strftime("%H:%M:%S"),
            )
            for schedule in schedules
        ]

    def build_config_meta(self, watcher_component: WatcherComponent) -> ConfigMeta:
        return ConfigMeta(
            tag=watcher_component.tag,
            port=watcher_component.port,
            schedules=self.get_watcher_schedules(watcher_component),
            max_up_days=watcher_component.maxUpDays,
            need_to_up=watcher_component.needToUp,
            need_to_send_mail=watcher_component.needToSendMail,
        )

    def is_ancestor_or_equal(self, ancestor_path: str, path: str) -> bool:
        if not ancestor_path or not path:
            return False
        try:
            return os.path.commonpath([ancestor_path, path]) == ancestor_path
        except ValueError:
            return False

    def apply_watcher_mapping(self, comp: Component, watcher_components: Optional[Dict[str, WatcherComponent]] = None) -> bool:
        comp.watcher = False

        if not watcher_components:
            return False

        discovered_path = self.normalize_component_path(comp.path)

        for watcher_component in watcher_components.values():
            watcher_path = self.normalize_component_path(watcher_component.runScriptPath)

            if not watcher_path:
                continue

            if self.is_ancestor_or_equal(discovered_path, watcher_path):
                comp.comp_name = self.format_component_name(watcher_component.name)
                comp.watcher = True
                comp.config_meta = self.build_config_meta(watcher_component)
                return True

        return False

    def build_watcher_configured_component(self, watcher_component: WatcherComponent) -> Optional[Component]:
        watcher_path = self.normalize_component_path(watcher_component.runScriptPath)
        if not watcher_path:
            return None

        comp = Component()
        comp.comp_name = self.format_component_name(watcher_component.name)
        comp.path = watcher_path
        comp.last_run_time = self.get_latest_run_time(watcher_path)
        comp.watcher = True
        comp.config_meta = self.build_config_meta(watcher_component)
        return comp

    def add_unidentified_watcher_components(self, watcher_components: Optional[Dict[str, WatcherComponent]] = None):
        if not watcher_components:
            return

        discovered_paths = [
            self.normalize_component_path(comp.path)
            for comp in self.components
            if self.normalize_component_path(comp.path)
        ]

        for watcher_component in watcher_components.values():
            watcher_path = self.normalize_component_path(watcher_component.runScriptPath)
            if not watcher_path:
                continue
            if any(self.is_ancestor_or_equal(discovered_path, watcher_path) for discovered_path in discovered_paths):
                continue

            comp = self.build_watcher_configured_component(watcher_component)
            if comp:
                self.components.append(comp)
                discovered_paths.append(watcher_path)

    def load_app_config(self, path):
        if not os.path.isfile(path):
            self.logger.error(f"Config file not found: {path}")
            return {}
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return yaml.safe_load(f) or {}
        except Exception as e:
            self.logger.exception(f"Error loading config: {e}")
            return {}

    def is_excluded_directory(self, dir_name):
        return any(keyword in dir_name.lower() for keyword in self.exclude_keywords)

    def get_valid_directories(self, dir_path):
        try:
            entries = os.listdir(dir_path)
        except OSError:
            return []
        return [
            os.path.join(dir_path, e) for e in entries
            if os.path.isdir(os.path.join(dir_path, e))
            and not self.is_excluded_directory(e)
        ]

    def get_latest_run_time(self, dir_path):
        latest = None
        for ld in ('logs', 'log'):
            log_dir = os.path.join(dir_path, ld)
            if os.path.isdir(log_dir):
                for fname in os.listdir(log_dir):
                    fp = os.path.join(log_dir, fname)
                    if os.path.isfile(fp):
                        try:
                            mtime = os.path.getmtime(fp)
                        except OSError:
                            continue
                        if latest is None or mtime > latest:
                            latest = mtime
        if latest is not None:
            return datetime.datetime.fromtimestamp(latest)
        return None

    def find_component_log_directory(self, dir_path) -> Optional[str]:
        for ld in ('logs', 'log'):
            log_dir = os.path.join(dir_path, ld)
            if os.path.isdir(log_dir):
                return log_dir
        return None

    def parse_simple_yaml(self, yml_file):
        data = {}
        description_lines = []
        in_description = False
        desc_indent = None
        with io.open(yml_file, 'r', encoding='utf-8') as f:
            for line in f:
                if not in_description:
                    match = re.match(r'^(\s*)([A-Za-z0-9_]+):\s*([>|])\s*$', line)
                    if match:
                        in_description = True
                        desc_indent = len(match.group(1))
                        continue
                else:
                    indent = len(line) - len(line.lstrip(' '))
                    if line.strip() and indent > desc_indent:
                        description_lines.append(line.strip())
                        continue
                    else:
                        data['description'] = ' '.join(description_lines)
                        in_description = False
                        description_lines = []
                        desc_indent = None
                stripped = line.strip()
                if not stripped or stripped.startswith('#') or ':' not in stripped:
                    continue
                key, val = stripped.split(':', 1)
                data[key.strip()] = val.strip().strip('"').strip("'")
        if in_description and description_lines:
            data['description'] = ' '.join(description_lines)
        return data

    def parse_release_date(self, value: Optional[str]) -> Optional[datetime.datetime]:
        if not value:
            return None

        raw_value = str(value).strip()
        if not raw_value:
            return None

        try:
            return datetime.datetime.strptime(raw_value, '%Y-%m-%d')
        except ValueError:
            pass

        normalized_value = raw_value.replace('Z', '+00:00')
        try:
            return datetime.datetime.fromisoformat(normalized_value)
        except ValueError as exc:
            raise ValueError(
                "Invalid release_date format: "
                f"{raw_value!r}. Expected YYYY-MM-DD or ISO-8601 timestamp."
            ) from exc
    shell_config_value_pattern = re.compile(r'^\s*(COMPNAME|PORT|AUTO_ADD_TO_WATCHER)\s*=\s*(".*?"|\'.*?\'|\S+)')

    def read_shell_config_values(self, config_sh_path):
        values = {}
        try:
            with io.open(config_sh_path, 'r', encoding='utf-8', errors='ignore') as f:
                for line in f:
                    stripped = line.strip()
                    if not stripped or stripped.startswith('#'):
                        continue
                    match = self.shell_config_value_pattern.match(line)
                    if not match:
                        continue
                    key, raw_value = match.group(1), match.group(2)
                    if raw_value[:1] in ('"', "'"):
                        value = raw_value.strip('"').strip("'")
                    else:
                        value = raw_value.split('#', 1)[0].strip()
                    if value:
                        values[key] = value
        except OSError:
            return {}
        return values

    def find_shell_config_file(self, dir_path):
        for candidate in (
            os.path.join(dir_path, 'config.sh'),
            os.path.join(dir_path, 'bin', 'config.sh'),
        ):
            if os.path.isfile(candidate):
                return candidate
        return None

    def read_component_shell_config(self, dir_path):
        config_sh_path = self.find_shell_config_file(dir_path)
        if not config_sh_path:
            return {'tag': None, 'port': None, 'auto_add': False, 'in_bin': False}

        values = self.read_shell_config_values(config_sh_path)
        tag = values.get('COMPNAME')
        port = None
        if 'PORT' in values:
            try:
                port = int(values['PORT'])
            except ValueError:
                port = None
        auto_add = str(values.get('AUTO_ADD_TO_WATCHER', '')).strip().lower() in ('true', 'yes', '1')
        in_bin = os.path.basename(os.path.dirname(config_sh_path)) == 'bin'
        return {'tag': tag, 'port': port, 'auto_add': auto_add, 'in_bin': in_bin}

    def apply_shell_config(self, dir_path, comp: Component) -> Component:
        shell_config = self.read_component_shell_config(dir_path)
        tag, port = shell_config['tag'], shell_config['port']
        if tag or port is not None:
            comp.config_meta = ConfigMeta(tag=tag, port=port)
        if tag:
            comp.comp_name = self.format_component_name(tag)
        return comp

    def has_component_children(self, dir_path):
        for child_dir in self.get_valid_directories(dir_path):
            if self.identify_component(child_dir):
                return True
        return False

    def identify_yaml_component(self, dir_path, comp: Component):
        yml_file = os.path.join(dir_path, 'component-details', 'component.yml')
        if not os.path.isfile(yml_file):
            return None

        data = self.parse_simple_yaml(yml_file)
        name = os.path.basename(dir_path)
        comp.comp_name = self.format_component_name(data.get('name', name))
        comp.category = self.normalize_category(data.get('category'))
        comp.platform = data.get('platform', 'Unknown')
        comp.version = data.get('version_tag', None)
        comp.pipeline = True if str(data.get('pipeline')).lower() == 'true' else False
        comp.description = data.get('description', None)
        comp.previous_tag = data.get('previous_tag', None)
        comp.release_date = self.parse_release_date(data.get('release_date'))
        comp.code_repo_url = data.get('code_repo_url', None)
        comp.config_repo_url = data.get('config_repo_url', None)
        comp.script_repo_url = data.get('script_repo_url', None)
        return comp

    def identify_solr_component(self, dir_path, comp: Component):
        name = os.path.basename(dir_path)
        if 'solr-' not in name.lower():
            return None

        server_path = os.path.join(dir_path, 'server')
        server_lib_path = os.path.join(server_path, 'lib')
        if not os.path.isdir(server_lib_path):
            return None

        try:
            has_jar = any(
                os.path.isfile(os.path.join(server_lib_path, file_name)) and file_name.endswith('.jar')
                for file_name in os.listdir(server_lib_path)
            )
        except OSError:
            return None

        if not has_jar:
            return None

        comp.comp_name = self.format_component_name(name)
        comp.category = 'tool'
        comp.path = dir_path
        comp.platform = 'Solr'
        comp.version = self.get_solr_version(dir_path)
        comp.last_run_time = self.get_latest_run_time(server_path)
        return comp

    def get_solr_version(self, dir_path):
        solr_bin = os.path.join(dir_path, 'bin', 'solr')
        if not os.path.isfile(solr_bin):
            return None

        for command in ([solr_bin, 'version'], [solr_bin, '--version']):
            try:
                result = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    timeout=SUBPROCESS_TIMEOUT,
                    check=False,
                )
            except OSError:
                return None

            output = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part).strip()
            version_match = re.search(r"Solr version is:\s*(\d+(?:\.\d+)+)", output, re.IGNORECASE)
            if not version_match:
                version_match = re.search(r"^(\d+(?:\.\d+)+)$", output, re.MULTILINE)
            if not version_match:
                version_match = re.search(r"(\d+(?:\.\d+)+)", output)

            if result.returncode == 0 and version_match:
                return version_match.group(1)

            if result.returncode == 0 and output:
                return output

            self.logger.debug(
                f"Failed to get Solr version for {dir_path} using {' '.join(command[1:])}: "
                f"exit={result.returncode} output={output}"
            )

        return None

    def identify_redis_component(self, dir_path, comp: Component):
        name = os.path.basename(dir_path)
        if 'redis' not in name.lower():
            return None

        log_path = os.path.join(dir_path, 'log')
        logs_path = os.path.join(dir_path, 'logs')
        memdump_path = os.path.join(dir_path, 'memdump')
        server_path = os.path.join(dir_path, 'server')
        redis_cli_path = os.path.join(server_path, 'redis-cli')
        redis_server_path = os.path.join(server_path, 'redis-server')

        if not (
            (os.path.isdir(log_path) or os.path.isdir(logs_path))
            and os.path.isdir(memdump_path)
            and os.path.isdir(server_path)
            and os.path.isfile(redis_cli_path)
            and os.path.isfile(redis_server_path)
        ):
            return None

        comp.comp_name = self.format_component_name(name)
        comp.category = 'tool'
        comp.path = dir_path
        comp.platform = 'Redis'
        comp.version = self.get_redis_version(dir_path)
        comp.last_run_time = self.get_latest_run_time(dir_path)
        return comp

    def get_redis_version(self, dir_path):
        redis_server_path = os.path.join(dir_path, 'server', 'redis-server')
        if not os.path.isfile(redis_server_path):
            return None

        try:
            result = subprocess.run(
                [redis_server_path, '--version'],
                capture_output=True,
                text=True,
                timeout=SUBPROCESS_TIMEOUT,
                check=False,
            )
        except OSError:
            return None

        output = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part).strip()
        version_match = re.search(r"Redis server\s+v=(\d+(?:\.\d+)+)", output, re.IGNORECASE)
        if not version_match:
            version_match = re.search(r"v=(\d+(?:\.\d+)+)", output, re.IGNORECASE)

        if result.returncode == 0 and version_match:
            return version_match.group(1)

        if result.returncode == 0 and output:
            return output

        self.logger.debug(
            f"Failed to get Redis version for {dir_path}: "
            f"exit={result.returncode} output={output}"
        )

        return None

    def identify_standard_component(self, dir_path, comp: Component):
        config_path = os.path.join(dir_path, 'config')
        log_path = os.path.join(dir_path, 'log')
        logs_path = os.path.join(dir_path, 'logs')
        if not (os.path.isdir(config_path) and (os.path.isdir(log_path) or os.path.isdir(logs_path))):
            return None

        name = os.path.basename(dir_path)
        comp.category = 'tool' if self.is_tool_component_name(name) else 'component'

        try:
            files = os.listdir(dir_path)
        except OSError:
            return None

        jar_files = [f for f in files if f.endswith('.jar')]
        pattern = re.compile(r'X[._]X')
        if len(jar_files) == 1:
            comp.version = os.path.splitext(jar_files[0])[0]
        elif len(jar_files) > 1:
            matching = [f for f in jar_files if pattern.search(f)]
            if len(matching) == 1:
                comp.version = os.path.splitext(matching[0])[0]

        for file in files:
            for extension, ext_platform in self.file_extensions.items():
                if file.endswith(extension):
                    comp.platform = ext_platform
                    return comp

        for sub_dir in ['bin', 'lib']:
            sub_dir_path = os.path.join(dir_path, sub_dir)
            if os.path.isdir(sub_dir_path):
                try:
                    inner_files = os.listdir(sub_dir_path)
                except OSError:
                    continue
                for file in inner_files:
                    for extension, ext_platform in self.file_extensions.items():
                        if file.endswith(extension):
                            comp.platform = ext_platform
                            return comp
                    if self.so_pattern.search(file):
                        comp.platform = 'C++'
                        return comp

        return None

    def identify_component(self, dir_path):
        name = os.path.basename(dir_path)
        comp = Component()
        comp.comp_name = self.format_component_name(name)
        comp.path = dir_path
        comp.last_run_time = self.get_latest_run_time(dir_path)

        yaml_comp = self.identify_yaml_component(dir_path, comp)
        if yaml_comp:
            return self.apply_shell_config(dir_path, yaml_comp)

        standard_comp = self.identify_standard_component(dir_path, comp)
        if standard_comp:
            return self.apply_shell_config(dir_path, standard_comp)

        if 'redis' in name.lower():
            redis_comp = self.identify_redis_component(dir_path, comp)
            if redis_comp:
                return self.apply_shell_config(dir_path, redis_comp)

        if 'solr' in name.lower():
            solr_comp = self.identify_solr_component(dir_path, comp)
            if solr_comp:
                return self.apply_shell_config(dir_path, solr_comp)

        return None

    def traverse_directory(self, dir_path, watcher_components=None, depth=1):
        if depth > self.max_depth:
            return

        for sub in self.get_valid_directories(dir_path):
            comp = self.identify_component(sub)
            has_component_children = self.has_component_children(sub)
            if comp:
                self.apply_watcher_mapping(comp, watcher_components)
                self.components.append(comp)
            if has_component_children or not comp:
                self.traverse_directory(sub, watcher_components, depth + 1)

    def gather_components(self, watcher_components=None):
        self.components = []
        self.traverse_directory(self.base_dir, watcher_components=watcher_components)
        self.add_unidentified_watcher_components(watcher_components=watcher_components)
        self.status = 'success'

    @staticmethod
    def _slugify_section_base(name):
        slug = re.sub(r'[\s-]+', '_', str(name or '').strip().upper())
        slug = re.sub(r'[^A-Z0-9_]', '', slug)
        slug = re.sub(r'_+', '_', slug).strip('_')
        return slug

    @classmethod
    def _generate_section_id(cls, name, used_ids):
        base = cls._slugify_section_base(name) or "COMPONENT"
        if base not in used_ids:
            return base
        suffix = 1
        while f"{base}_{suffix}" in used_ids:
            suffix += 1
        return f"{base}_{suffix}"

    def auto_register_components(self, watcher_components: Optional[Dict[str, WatcherComponent]] = None) -> List[str]:
        self.gather_components(watcher_components=watcher_components)
        used_ids = {wc.id for wc in (watcher_components or {}).values() if wc.id}
        added_section_ids: List[str] = []

        for comp in self.components:
            if comp.watcher or not comp.path:
                continue

            shell_config = self.read_component_shell_config(comp.path)
            if not shell_config['auto_add'] or not shell_config['tag']:
                continue

            section_id = self._generate_section_id(comp.comp_name, used_ids)
            run_script_path = os.path.join(comp.path, 'bin') if shell_config['in_bin'] else comp.path
            log_directory = self.find_component_log_directory(comp.path)

            entry = ComponentConfigEntry(
                section_id=section_id,
                tag=shell_config['tag'],
                name=comp.comp_name,
                port=shell_config['port'],
                startTime="00:00:00",
                endTime="23:59:00",
                runningDates=list(range(7)),
                maxUpDays=7,
                needToUp=True,
                needToSendMail=True,
                runScriptPath=run_script_path,
                runScript="run.sh",
                logDirectory=log_directory,
            )

            try:
                config_writer.apply_changes([entry], [])
            except (ValueError, FileNotFoundError) as exc:
                self.logger.warning(
                    f"Could not auto-add component {comp.comp_name!r} (tag={shell_config['tag']!r}) "
                    f"from {comp.path}/config.sh: {exc}"
                )
                continue

            used_ids.add(section_id)
            added_section_ids.append(section_id)
            self.logger.warning(
                f"Auto-added component {comp.comp_name!r} (tag={shell_config['tag']!r}) as [{section_id}] "
                f"from {comp.path} (config.sh sets AUTO_ADD_TO_WATCHER=true)."
            )

        return added_section_ids

    def send_to_backend(self, payload):
        try:
            json.dumps(payload, allow_nan=False)
        except TypeError as e:
            self.logger.exception(f"Payload not JSON serializable: {e}")
            return False

        max_attempts = 5
        for attempt in range(1, max_attempts + 1):
            try:
                self.logger.info(
                    f"Attempt {attempt}/{max_attempts}: sending {len(payload.get('components', []))} components "
                    f"to {self.component_db_ingest_url}"
                )
                resp = requests.post(self.component_db_ingest_url, json=payload, timeout=3)

                if 200 <= resp.status_code < 300:
                    self.logger.info(f"Send successful (HTTP {resp.status_code})")
                    return True

                if 400 <= resp.status_code < 500 and resp.status_code != 429:
                    self.logger.warning(f"Send failed (HTTP {resp.status_code}): {resp.text[:500]}")
                    return False

                self.logger.warning(f"Send failed (HTTP {resp.status_code}): {resp.text[:500]}")

            except Exception as e:
                self.logger.warning(f"Send attempt {attempt}/{max_attempts} failed: {e}")

            if attempt < max_attempts:
                self.logger.debug("Sleeping 5 seconds before next attempt...")
                time.sleep(5)

        self.logger.error(f"All {max_attempts} attempts to send to backend failed")
        return False

    def run(self, watcher_components: Optional[Dict[str, WatcherComponent]] = None):
        try:
            started = time.monotonic()
            self.logger.info(f"Scanning {self.base_dir} for components (max depth {self.max_depth})...")
            self.gather_components(watcher_components=watcher_components)
            elapsed = time.monotonic() - started

            if self.components:
                self.logger.info(
                    f"Collected {len(self.components)} component(s) in {elapsed:.1f}s: "
                    f"{', '.join(sorted(str(comp.comp_name) for comp in self.components))}"
                )
            else:
                self.logger.warning(f"No components found under {self.base_dir} after {elapsed:.1f}s.")

            for comp in self.components:
                self.logger.debug(comp.model_dump_json())

            payload_model = ComponentCollectorRequest(
                status=self.status,
                ip=var.SERVER_IP,
                region=self.region or "",
                components=self.components,
                message=self.message
            )

            out = payload_model.model_dump(mode="json")

            if self.send_component_details and self.component_db_ingest_url:
                if not self.region:
                    self.logger.error(
                        "Region is required by backend but not set. "
                        "Add aggregator_backend.region in config or set the REGION env var."
                    )
                else:
                    self.send_to_backend(out)
            else:
                self.logger.debug(
                    f"Backend send skipped (send_component_details={self.send_component_details}, "
                    f"url={self.component_db_ingest_url!r})."
                )

        except Exception:
            self.logger.exception("Component collection run failed.")
