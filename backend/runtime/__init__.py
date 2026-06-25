"""Task runtime + sandbox policy primitives.

Public re-exports so callers can write::

    from backend.runtime import TaskRuntime, SandboxPolicy, HostTaskRuntime

without caring which submodule a name lives in.
"""
from .base import RuntimeKind, RuntimeResult, TaskRuntime
from .host_runtime import HostTaskRuntime
from .policy import (
    DEFAULT_POLICY,
    GLOBAL_OUTPUT_CEILING_BYTES,
    GLOBAL_TIMEOUT_CEILING_SECONDS,
    PolicyViolation,
    SandboxPolicy,
    check_path_under,
    writable_paths_match,
)

__all__ = [
    "DEFAULT_POLICY",
    "GLOBAL_OUTPUT_CEILING_BYTES",
    "GLOBAL_TIMEOUT_CEILING_SECONDS",
    "HostTaskRuntime",
    "PolicyViolation",
    "RuntimeKind",
    "RuntimeResult",
    "SandboxPolicy",
    "TaskRuntime",
    "check_path_under",
    "writable_paths_match",
]
