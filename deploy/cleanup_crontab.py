#!/usr/bin/env python3
import re
import sys
from pathlib import Path


WATCHER_BLOCK_HEADER = "################## ALL IN ONE WATCHER ####################"


def cleanup_crontab(content: str, pattern) -> str:
    cleaned_lines = []
    skip_blank_after_removed = False

    for raw_line in content.splitlines():
        line = raw_line.rstrip("\n")
        if pattern.search(line):
            if cleaned_lines and cleaned_lines[-1] == "":
                cleaned_lines.pop()
            skip_blank_after_removed = True
            continue

        if skip_blank_after_removed and line.strip() == "":
            continue

        skip_blank_after_removed = False
        cleaned_lines.append(line)

    while cleaned_lines and cleaned_lines[-1] == "":
        cleaned_lines.pop()

    return "\n".join(cleaned_lines) + ("\n" if cleaned_lines else "")


def append_watcher_block(content: str, watcher_path: str) -> str:
    watcher_lines = [
        WATCHER_BLOCK_HEADER,
        "* * * * * {}/monitor.sh".format(watcher_path),
        "00 00 * * * {}/restart.sh".format(watcher_path),
    ]

    existing_content = content.rstrip("\n")
    if existing_content:
        return existing_content + "\n\n" + "\n".join(watcher_lines) + "\n"

    return "\n".join(watcher_lines) + "\n"


def main() -> None:
    if len(sys.argv) != 5:
        raise SystemExit(
            "usage: cleanup_crontab.py "
            "<backup_crontab_path> <updated_crontab_path> <remove_pattern> <watcher_path>"
        )

    backup_crontab_path = Path(sys.argv[1])
    updated_crontab_path = Path(sys.argv[2])
    remove_pattern = sys.argv[3]
    watcher_path = sys.argv[4]
    pattern = re.compile(
        rf"{remove_pattern}|ALL IN ONE WATCHER",
        re.IGNORECASE,
    )

    current_crontab = backup_crontab_path.read_text(encoding="utf-8")
    cleaned_crontab = cleanup_crontab(current_crontab, pattern)
    updated_crontab = append_watcher_block(cleaned_crontab, watcher_path)
    updated_crontab_path.write_text(updated_crontab, encoding="utf-8")


if __name__ == "__main__":
    main()
