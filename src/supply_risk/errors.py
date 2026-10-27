class DataError(ValueError):
    """Invalid input or an immutable version conflict."""


class InfrastructureError(RuntimeError):
    """Storage unavailable; never interpreted as a negative assessment."""
