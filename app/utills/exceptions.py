class WatcherError(Exception):
    """Base watcher exception."""


class ConfigLoadError(WatcherError):
    """Raised when config loading fails."""


class SecretRetrievalError(WatcherError):
    """Raised when secret retrieval fails."""
