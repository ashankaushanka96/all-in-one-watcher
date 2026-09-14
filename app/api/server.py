import re
import subprocess
import time
from pathlib import Path
from typing import List, Literal, Optional

import psutil
import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from loguru import logger

import app.variables as var
from app.api import config_writer, log_reader
from app.api.auth import load_watcher_api_key
from app.api.models import ComponentActionRequest, ConfigureRequest, RollbackRequest

api_logger = logger.bind(comp_name="WatcherAPI")

SCRIPT_TIMEOUT_SECONDS = 30
STATE_POLL_TIMEOUT_SECONDS = 15.0
STATE_POLL_INTERVAL_SECONDS = 0.3
STATE_CONFIRM_SECONDS = 4.0


def _script_path(script_name: str) -> Path:
    return Path(var.WATCHER_DIRECTORY) / script_name


def _log_script_result(label: str, result: subprocess.CompletedProcess) -> None:
    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()
    if result.returncode == 0:
        api_logger.debug(f"{label} finished (exit=0). stdout={stdout!r}")
    else:
        api_logger.error(f"{label} failed (exit={result.returncode}). stdout={stdout!r} stderr={stderr!r}")


def _run_script(
    script_name: Literal["run.sh", "kill.sh", "restart.sh"],
    args: Optional[List[str]] = None,
) -> subprocess.CompletedProcess:
    """Runs run.sh/kill.sh/restart.sh and waits for it to finish; safe to block since this API is its own process."""
    script_path = _script_path(script_name)
    if not script_path.exists():
        raise FileNotFoundError(f"{script_name} not found at {script_path}")
    result = subprocess.run(
        [str(script_path), *(args or [])],
        cwd=var.WATCHER_DIRECTORY,
        capture_output=True,
        text=True,
        timeout=SCRIPT_TIMEOUT_SECONDS,
    )
    _log_script_result(f"{script_name} {' '.join(args or [])}".strip(), result)
    return result


def _is_watcher_running() -> bool:
    # Matches by subcommand ("watcher"/legacy "run"), not filename, so it can't be fooled by the API's own process.
    result = subprocess.run(
        ["bash", "-c", r"ps -ef | grep -E 'all_in_one_watcher(\.py|\.bin)?[[:space:]]+(run|watcher)([[:space:]]|$)' | grep -v grep"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    return bool(result.stdout.strip())


def _is_watcher_running_confirmed(confirm_after: float = STATE_CONFIRM_SECONDS) -> bool:
    """True only if the watcher is running now and still running a moment later (avoids a crash-on-startup false positive)."""
    if not _is_watcher_running():
        return False
    time.sleep(confirm_after)
    return _is_watcher_running()


def _poll_until_running_state(
    expected: bool,
    timeout: float = STATE_POLL_TIMEOUT_SECONDS,
    interval: float = STATE_POLL_INTERVAL_SECONDS,
) -> bool:
    """Polls until the watcher settles into the expected running state, since run.sh/kill.sh return before it's actually up or down."""
    check = _is_watcher_running_confirmed if expected else _is_watcher_running
    deadline = time.monotonic() + timeout
    observed = check()
    while observed != expected and time.monotonic() < deadline:
        time.sleep(interval)
        observed = check()
    return observed


def _clear_disable_flag() -> bool:
    disable_flag = Path(var.WATCHER_DIRECTORY) / "stop_watcher"
    if disable_flag.exists():
        disable_flag.unlink()
        return True
    return False


def _run_script_in_dir(script_dir: Path, script_name: str) -> subprocess.CompletedProcess:
    """Like _run_script, but for a component's own directory (each component ships its own run.sh/kill.sh/restart.sh)."""
    script_path = script_dir / script_name
    if not script_path.exists():
        raise FileNotFoundError(f"{script_name} not found at {script_path}")
    result = subprocess.run(
        ["bash", script_name],
        cwd=str(script_dir),
        capture_output=True,
        text=True,
        timeout=SCRIPT_TIMEOUT_SECONDS,
    )
    _log_script_result(f"{script_dir}/{script_name}", result)
    return result


def _is_component_running(tag: str) -> bool:
    # Same tag-based process match as component_watcher.py's _pid, via psutil rather than a shell (no injection surface for an API-supplied tag).
    pattern = re.compile(rf'\b{re.escape(tag)}\b|-D{re.escape(tag)}\b')
    watcher_entry_point = re.compile(r'all_in_one_watcher(\.py|\.bin)?\s+api(\s|$)')
    for proc in psutil.process_iter(['cmdline', 'name']):
        try:
            cmdline = ' '.join(proc.info.get('cmdline') or [])
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
        name = proc.info.get('name') or ''
        if not (pattern.search(cmdline) or pattern.search(name)):
            continue
        if watcher_entry_point.search(cmdline):
            continue
        return True
    return False


def _is_component_running_confirmed(tag: str, confirm_after: float = STATE_CONFIRM_SECONDS) -> bool:
    if not _is_component_running(tag):
        return False
    time.sleep(confirm_after)
    return _is_component_running(tag)


def _poll_until_component_running(
    tag: str,
    expected: bool,
    timeout: float = STATE_POLL_TIMEOUT_SECONDS,
    interval: float = STATE_POLL_INTERVAL_SECONDS,
) -> bool:
    check = (lambda: _is_component_running_confirmed(tag)) if expected else (lambda: _is_component_running(tag))
    deadline = time.monotonic() + timeout
    observed = check()
    while observed != expected and time.monotonic() < deadline:
        time.sleep(interval)
        observed = check()
    return observed


def _resolve_component_script_dir(tag: str):
    try:
        entry = config_writer.find_component_by_tag(tag)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    if not entry["runScriptPath"]:
        raise HTTPException(status_code=404, detail=f"No runScriptPath configured for component tag {tag!r}.")
    script_dir = Path(entry["runScriptPath"])
    if not script_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"Run script directory not found: {script_dir}")
    return entry, script_dir


def _resolve_component_log_path(tag: str, file_name: str) -> Path:
    try:
        entry = config_writer.find_component_by_tag(tag)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    if not entry["logDirectory"]:
        raise HTTPException(status_code=404, detail=f"No log directory configured for component tag {tag!r}.")
    try:
        return log_reader.resolve_log_file(entry["logDirectory"], file_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


def create_app(expected_api_key: str) -> FastAPI:
    app = FastAPI(title="All-in-one-watcher API", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def log_requests(request: Request, call_next):
        started = time.monotonic()
        client = request.client.host if request.client else "unknown"
        try:
            response = await call_next(request)
        except Exception:
            elapsed_ms = (time.monotonic() - started) * 1000
            api_logger.exception(
                f"{request.method} {request.url.path} from {client} raised after {elapsed_ms:.0f}ms"
            )
            raise

        elapsed_ms = (time.monotonic() - started) * 1000
        message = (
            f"{request.method} {request.url.path} from {client} "
            f"-> {response.status_code} in {elapsed_ms:.0f}ms"
        )
        if response.status_code >= 500:
            api_logger.error(message)
        elif response.status_code >= 400:
            api_logger.warning(message)
        elif request.method == "GET":
            api_logger.debug(message)
        else:
            api_logger.info(message)
        return response

    def verify_api_key(x_watcher_api_key: Optional[str] = Header(None, alias="X-Watcher-Api-Key")):
        if not expected_api_key or x_watcher_api_key != expected_api_key:
            raise HTTPException(status_code=401, detail="Invalid or missing watcher API key")
        return True

    @app.get("/api/v1/watcher/version")
    def get_version(_: bool = Depends(verify_api_key)):
        version_path = Path(var.WATCHER_DIRECTORY) / "version.txt"
        version = "unknown"
        if version_path.exists():
            content = version_path.read_text(encoding="utf-8").strip()
            if content:
                version = content
        return {"version": version}

    @app.get("/api/v1/watcher/status")
    def get_status(_: bool = Depends(verify_api_key)):
        return {"running": _is_watcher_running()}

    @app.get("/api/v1/watcher/components")
    def get_components(_: bool = Depends(verify_api_key)):
        try:
            return {
                "components": config_writer.read_raw_components(),
                "has_backup": config_writer.has_backup(),
            }
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

    @app.post("/api/v1/watcher/start")
    def start_watcher(_: bool = Depends(verify_api_key)):
        api_logger.warning("Start requested via watcher API.")
        if _is_watcher_running():
            return {"status": "already_running", "running": True}

        _clear_disable_flag()
        try:
            _run_script("run.sh", ["--watcher"])
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            raise HTTPException(status_code=500, detail=f"Failed to start watcher: {exc}")

        running = _poll_until_running_state(True)
        if not running:
            api_logger.error("Watcher did not come up after start; check its own logs on the host.")
            raise HTTPException(
                status_code=502,
                detail="Watcher process did not come up after start; check its logs on the host.",
            )
        return {"status": "started", "running": True}

    @app.post("/api/v1/watcher/stop")
    def stop_watcher(_: bool = Depends(verify_api_key)):
        api_logger.warning("Stop requested via watcher API.")
        try:
            _run_script("kill.sh", ["--stop"])
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            raise HTTPException(status_code=500, detail=f"Failed to stop watcher: {exc}")

        running = _poll_until_running_state(False)
        if running:
            api_logger.error("Watcher is still running after stop.")
            raise HTTPException(status_code=502, detail="Watcher process is still running after stop.")
        return {"status": "stopped", "running": False}

    @app.post("/api/v1/watcher/restart")
    def restart_watcher(_: bool = Depends(verify_api_key)):
        api_logger.warning("Restart requested via watcher API.")
        _clear_disable_flag()
        try:
            _run_script("restart.sh", ["--watcher"])
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            raise HTTPException(status_code=500, detail=f"Failed to restart watcher: {exc}")

        running = _poll_until_running_state(True)
        if not running:
            api_logger.error("Watcher did not come back up after restart; check its own logs on the host.")
            raise HTTPException(
                status_code=502,
                detail="Watcher did not come back up after restart; check its logs on the host.",
            )
        return {"status": "restarted", "running": True}

    @app.post("/api/v1/watcher/configure")
    def configure_watcher(req: ConfigureRequest, _: bool = Depends(verify_api_key)):
        api_logger.warning(
            f"Configure requested via watcher API: "
            f"add={[entry.section_id for entry in req.add_components]} "
            f"remove={req.remove_component_ids}"
        )
        try:
            added, removed = config_writer.apply_changes(req.add_components, req.remove_component_ids)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        restarted = False
        if req.restart and (added or removed):
            _clear_disable_flag()
            try:
                _run_script("restart.sh", ["--watcher"])
                restarted = _poll_until_running_state(True)
            except (FileNotFoundError, subprocess.TimeoutExpired):
                api_logger.exception("Failed to restart watcher after configure.")
                restarted = False

        return {
            "status": "success",
            "added": added,
            "removed": removed,
            "restarted": restarted,
        }

    @app.post("/api/v1/watcher/rollback")
    def rollback_watcher(req: RollbackRequest, _: bool = Depends(verify_api_key)):
        api_logger.warning("Rollback requested via watcher API.")
        try:
            diff = config_writer.rollback_changes()
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        restarted = False
        if req.restart:
            _clear_disable_flag()
            try:
                _run_script("restart.sh", ["--watcher"])
                restarted = _poll_until_running_state(True)
            except (FileNotFoundError, subprocess.TimeoutExpired):
                api_logger.exception("Failed to restart watcher after rollback.")
                restarted = False

        return {
            "status": "success",
            "restored": diff["restored"],
            "removed": diff["removed"],
            "changed": diff["changed"],
            "restarted": restarted,
        }

    @app.get("/api/v1/watcher/component/status")
    def get_component_status(tag: str, _: bool = Depends(verify_api_key)):
        try:
            config_writer.find_component_by_tag(tag)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        return {"running": _is_component_running(tag)}

    @app.post("/api/v1/watcher/component/start")
    def start_component(req: ComponentActionRequest, _: bool = Depends(verify_api_key)):
        api_logger.warning(f"Component start requested via watcher API: tag={req.tag}")
        entry, script_dir = _resolve_component_script_dir(req.tag)

        if _is_component_running(req.tag):
            return {"status": "already_running", "running": True}

        try:
            _run_script_in_dir(script_dir, entry["runScript"])
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            raise HTTPException(status_code=500, detail=f"Failed to start component: {exc}")

        running = _poll_until_component_running(req.tag, True)
        if not running:
            api_logger.error(f"Component {req.tag!r} did not come up after start; check its own logs.")
            raise HTTPException(
                status_code=502,
                detail=f"Component {req.tag!r} did not come up after start; check its logs on the host.",
            )
        return {"status": "started", "running": True}

    @app.post("/api/v1/watcher/component/stop")
    def stop_component(req: ComponentActionRequest, _: bool = Depends(verify_api_key)):
        api_logger.warning(f"Component stop requested via watcher API: tag={req.tag}")
        _entry, script_dir = _resolve_component_script_dir(req.tag)

        try:
            _run_script_in_dir(script_dir, "kill.sh")
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            raise HTTPException(status_code=500, detail=f"Failed to stop component: {exc}")

        running = _poll_until_component_running(req.tag, False)
        if running:
            api_logger.error(f"Component {req.tag!r} is still running after stop.")
            raise HTTPException(status_code=502, detail=f"Component {req.tag!r} is still running after stop.")
        return {"status": "stopped", "running": False}

    @app.post("/api/v1/watcher/component/restart")
    def restart_component(req: ComponentActionRequest, _: bool = Depends(verify_api_key)):
        api_logger.warning(f"Component restart requested via watcher API: tag={req.tag}")
        _entry, script_dir = _resolve_component_script_dir(req.tag)

        try:
            _run_script_in_dir(script_dir, "restart.sh")
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            raise HTTPException(status_code=500, detail=f"Failed to restart component: {exc}")

        running = _poll_until_component_running(req.tag, True)
        if not running:
            api_logger.error(f"Component {req.tag!r} did not come back up after restart; check its own logs.")
            raise HTTPException(
                status_code=502,
                detail=f"Component {req.tag!r} did not come back up after restart; check its logs on the host.",
            )
        return {"status": "restarted", "running": True}

    @app.get("/api/v1/watcher/component/logs")
    def list_component_logs(tag: str, _: bool = Depends(verify_api_key)):
        try:
            entry = config_writer.find_component_by_tag(tag)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        if not entry["logDirectory"]:
            raise HTTPException(status_code=404, detail=f"No log directory configured for component tag {tag!r}.")
        return {"files": log_reader.list_log_files(entry["logDirectory"])}

    @app.get("/api/v1/watcher/component/logs/tail")
    def tail_component_log(
        tag: str,
        file: str,
        lines: int = log_reader.DEFAULT_TAIL_LINES,
        _: bool = Depends(verify_api_key),
    ):
        path = _resolve_component_log_path(tag, file)
        return log_reader.tail_lines(path, lines)

    @app.get("/api/v1/watcher/component/logs/page")
    def page_component_log(
        tag: str,
        file: str,
        offset: int = 0,
        lines: int = log_reader.DEFAULT_PAGE_LINES,
        _: bool = Depends(verify_api_key),
    ):
        path = _resolve_component_log_path(tag, file)
        return log_reader.read_page(path, offset, lines)

    @app.get("/api/v1/watcher/component/logs/grep")
    def grep_component_log(
        tag: str,
        file: str,
        pattern: List[str] = Query(...),
        offset: int = 0,
        lines: int = log_reader.DEFAULT_PAGE_LINES,
        ignore_case: bool = False,
        regex: bool = False,
        _: bool = Depends(verify_api_key),
    ):
        # Repeated ?pattern=a&pattern=b query params are ANDed, like chaining `grep a | grep b`.
        path = _resolve_component_log_path(tag, file)
        try:
            return log_reader.grep_lines(path, pattern, offset, lines, ignore_case=ignore_case, use_regex=regex)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.get("/api/v1/watcher/component/logs/download")
    def download_component_log(tag: str, file: str, _: bool = Depends(verify_api_key)):
        path = _resolve_component_log_path(tag, file)
        return FileResponse(path, filename=file, media_type="application/octet-stream")

    @app.get("/api/v1/watcher/component/logs/grep/download")
    def download_grep_component_log(
        tag: str,
        file: str,
        pattern: List[str] = Query(...),
        ignore_case: bool = False,
        regex: bool = False,
        _: bool = Depends(verify_api_key),
    ):
        path = _resolve_component_log_path(tag, file)
        try:
            lines = log_reader.grep_matching_lines(path, pattern, ignore_case=ignore_case, use_regex=regex)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        stem = Path(file).stem
        suffix = Path(file).suffix or ".txt"
        download_name = f"{stem}.filtered{suffix}"
        return StreamingResponse(
            lines,
            media_type="text/plain",
            headers={"Content-Disposition": f'attachment; filename="{download_name}"'},
        )

    return app


def run_watcher_api(settings) -> None:
    """Blocking entrypoint used by the "api" subcommand, its own OS process separate from the watcher."""
    api_settings = settings.watcher_api
    if not api_settings.enabled:
        api_logger.info("Watcher API is disabled in appconfig.yaml; exiting.")
        return

    api_key = load_watcher_api_key(api_settings, settings.meta_data.region)
    if not api_key:
        api_logger.error("No watcher API key could be loaded; watcher API will not start.")
        return

    app = create_app(api_key)
    api_logger.info(f"Watcher API listening on {api_settings.host}:{api_settings.port}")
    uvicorn.run(app, host=api_settings.host, port=api_settings.port, log_level="warning")
