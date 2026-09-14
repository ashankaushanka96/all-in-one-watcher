#!/usr/bin/env python3
"""Single entry point for the watcher, its control API, and its monitor check.

Usage:
    all_in_one_watcher.py watcher --config ./config/config.ini [--debug]
    all_in_one_watcher.py api [--config ./config/config.ini] [--debug]
    all_in_one_watcher.py monitor [--debug]
"""

if __name__ == "__main__":
    from app.cli import main
    main()
