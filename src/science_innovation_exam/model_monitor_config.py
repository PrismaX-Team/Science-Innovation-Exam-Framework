"""Stdlib-only operational policy, shared by host configuration and sandbox gateway."""
import math

from .common.errors import ContractError


MONITOR_DEFAULTS = {
    "enabled": True, "check_seconds": 5, "query_timeout_seconds": 5,
    "progress_write_seconds": 2,
    "observation_warning_seconds": 30, "observation_timeout_seconds": 120,
    "first_response_warning_seconds": 120, "first_response_timeout_seconds": 600,
    "idle_warning_seconds": 120, "idle_timeout_seconds": 600,
    "request_warning_seconds": 900, "request_timeout_seconds": 1800,
    "connect_timeout_seconds": 30, "retry_attempts": 0, "retry_backoff_seconds": 1,
}


def check_model_monitor(options):
    if set(options) != set(MONITOR_DEFAULTS):
        raise ContractError("unknown model_monitor fields")
    if not isinstance(options["enabled"], bool):
        raise ContractError("model_monitor.enabled must be boolean")
    for key, number in options.items():
        if key == "enabled":
            continue
        if key == "retry_attempts":
            if isinstance(number, bool) or not isinstance(number, int) or number < 0:
                raise ContractError("model_monitor.retry_attempts must be a nonnegative integer")
        elif isinstance(number, bool) or not isinstance(number, (float, int)) or not math.isfinite(number) or number <= 0:
            raise ContractError("model_monitor.%s must be positive" % key)
    for prefix in ("observation", "first_response", "idle", "request"):
        if options[prefix + "_warning_seconds"] > options[prefix + "_timeout_seconds"]:
            raise ContractError("model_monitor.%s warning must not exceed timeout" % prefix)
