import os
import re
from pathlib import Path
from typing import Callable, Dict, Iterator, List

DEFAULT_TAIL_LINES = 200
MAX_TAIL_LINES = 5000
DEFAULT_PAGE_LINES = 200
MAX_PAGE_LINES = 2000
MAX_GREP_SCAN_BYTES = 5 * 1024 * 1024  # caps a sparse-pattern scan in a huge file per request
READ_CHUNK_SIZE = 65536
TAIL_CHUNK_SIZE = 8192


def resolve_log_file(log_directory: str, file_name: str) -> Path:
    """Resolves a log file name against a component's log directory, refusing anything but a bare filename inside it."""
    if not file_name or os.path.basename(file_name) != file_name:
        raise ValueError(f"Invalid log file name: {file_name!r}")
    directory = Path(log_directory).resolve()
    candidate = (directory / file_name).resolve()
    try:
        candidate.relative_to(directory)
    except ValueError:
        raise ValueError(f"Invalid log file name: {file_name!r}")
    if not candidate.is_file():
        raise FileNotFoundError(f"Log file not found: {candidate}")
    return candidate


def list_log_files(log_directory: str) -> List[Dict]:
    directory = Path(log_directory)
    if not directory.is_dir():
        return []
    files = []
    for entry in directory.iterdir():
        if not entry.is_file():
            continue
        try:
            stat = entry.stat()
        except OSError:
            continue
        files.append({"name": entry.name, "size": stat.st_size, "modified": stat.st_mtime})
    files.sort(key=lambda f: f["modified"], reverse=True)
    return files


def tail_lines(path: Path, lines: int = DEFAULT_TAIL_LINES) -> Dict:
    """tail -n equivalent: reads backwards from EOF in chunks, without loading the whole file into memory."""
    lines = max(1, min(lines, MAX_TAIL_LINES))
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        file_size = f.tell()
        data = b""
        position = file_size
        while position > 0 and data.count(b"\n") <= lines:
            read_size = min(TAIL_CHUNK_SIZE, position)
            position -= read_size
            f.seek(position)
            data = f.read(read_size) + data
        text = data.decode("utf-8", errors="replace")
    result_lines = text.splitlines()[-lines:]
    return {"lines": result_lines, "total_size": file_size, "eof": True}


def read_page(path: Path, offset: int = 0, lines: int = DEFAULT_PAGE_LINES) -> Dict:
    """less-style forward paginated read, returning the exact byte offset to resume from (stateless paging)."""
    lines = max(1, min(lines, MAX_PAGE_LINES))
    file_size = path.stat().st_size
    offset = max(0, min(offset, file_size))
    result_lines: List[str] = []
    cursor = offset
    eof = False

    with open(path, "rb") as f:
        f.seek(offset)
        buffer = b""
        while len(result_lines) < lines:
            chunk = f.read(READ_CHUNK_SIZE)
            if not chunk:
                eof = True
                if buffer:
                    result_lines.append(buffer.decode("utf-8", errors="replace"))
                    cursor += len(buffer)
                break
            buffer += chunk
            while len(result_lines) < lines:
                newline_index = buffer.find(b"\n")
                if newline_index == -1:
                    break
                line_bytes = buffer[:newline_index]
                consumed = newline_index + 1
                buffer = buffer[consumed:]
                cursor += consumed
                result_lines.append(line_bytes.decode("utf-8", errors="replace"))

    eof = eof or cursor >= file_size
    return {"lines": result_lines, "next_offset": cursor, "total_size": file_size, "eof": eof}


def _build_line_test(
    patterns: List[str], ignore_case: bool = False, use_regex: bool = False
) -> Callable[[str], bool]:
    """Builds a single `test(line) -> bool` requiring every pattern to match, like chaining `grep a | grep b | grep c`."""
    if isinstance(patterns, str):
        patterns = [patterns]
    patterns = [p for p in patterns if p]
    if not patterns:
        raise ValueError("At least one search pattern is required.")

    testers = []
    if use_regex:
        flags = re.IGNORECASE if ignore_case else 0
        for pattern in patterns:
            try:
                testers.append(re.compile(pattern, flags).search)
            except re.error as exc:
                raise ValueError(f"Invalid regular expression {pattern!r}: {exc}") from exc
    else:
        for pattern in patterns:
            needle = pattern.lower() if ignore_case else pattern

            def make_test(needle=needle):
                def test(line: str) -> bool:
                    return needle in (line.lower() if ignore_case else line)

                return test

            testers.append(make_test())

    def test(line: str) -> bool:
        return all(tester(line) for tester in testers)

    return test


def grep_lines(
    path: Path,
    patterns: List[str],
    offset: int = 0,
    lines: int = DEFAULT_PAGE_LINES,
    ignore_case: bool = False,
    use_regex: bool = False,
) -> Dict:
    """less + grep equivalent, bounded by MAX_GREP_SCAN_BYTES per request."""
    test = _build_line_test(patterns, ignore_case, use_regex)
    lines = max(1, min(lines, MAX_PAGE_LINES))
    file_size = path.stat().st_size
    offset = max(0, min(offset, file_size))

    result_lines: List[str] = []
    cursor = offset
    scanned_bytes = 0
    eof = False

    with open(path, "rb") as f:
        f.seek(offset)
        buffer = b""
        while len(result_lines) < lines and scanned_bytes < MAX_GREP_SCAN_BYTES:
            chunk = f.read(READ_CHUNK_SIZE)
            if not chunk:
                eof = True
                if buffer:
                    text = buffer.decode("utf-8", errors="replace")
                    cursor += len(buffer)
                    if test(text):
                        result_lines.append(text)
                break
            scanned_bytes += len(chunk)
            buffer += chunk
            while len(result_lines) < lines:
                newline_index = buffer.find(b"\n")
                if newline_index == -1:
                    break
                line_bytes = buffer[:newline_index]
                consumed = newline_index + 1
                buffer = buffer[consumed:]
                cursor += consumed
                text = line_bytes.decode("utf-8", errors="replace")
                if test(text):
                    result_lines.append(text)

    eof = eof or cursor >= file_size
    return {
        "lines": result_lines,
        "next_offset": cursor,
        "total_size": file_size,
        "eof": eof,
        "scan_truncated": (not eof) and scanned_bytes >= MAX_GREP_SCAN_BYTES,
    }


def grep_matching_lines(
    path: Path,
    patterns: List[str],
    ignore_case: bool = False,
    use_regex: bool = False,
) -> Iterator[bytes]:
    """Streams every matching line to true EOF with no MAX_GREP_SCAN_BYTES cap, for a one-shot download rather than a page."""
    test = _build_line_test(patterns, ignore_case, use_regex)

    with open(path, "rb") as f:
        buffer = b""
        while True:
            chunk = f.read(READ_CHUNK_SIZE)
            if not chunk:
                if buffer:
                    text = buffer.decode("utf-8", errors="replace")
                    if test(text):
                        yield (text + "\n").encode("utf-8")
                break
            buffer += chunk
            while True:
                newline_index = buffer.find(b"\n")
                if newline_index == -1:
                    break
                line_bytes = buffer[:newline_index]
                buffer = buffer[newline_index + 1 :]
                text = line_bytes.decode("utf-8", errors="replace")
                if test(text):
                    yield (text + "\n").encode("utf-8")
