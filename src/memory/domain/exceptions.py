"""Domain-specific exceptions."""

class REQLError(Exception):
    """Base exception for REQL."""


class StorageError(REQLError):
    """Raised when the storage adapter cannot complete an operation."""


class StorageBusyError(StorageError):
    """Raised when another lease prevents access to storage."""
