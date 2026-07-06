"""
Centralized Exception Hierarchy for Flight Physics Pipeline.
"""

class PipelineError(RuntimeError):
    """Base class for all pipeline errors."""
    pass


class FetchingError(PipelineError):
    """Raised when a fetch operation fails unrecoverably."""
    pass


class CheckpointError(PipelineError):
    """Raised when a checkpoint cannot be read (never fatal — callers log and continue)."""
    pass


class RetryError(PipelineError):
    """Raised by retry_backoff when max_retries is exhausted."""
    pass
