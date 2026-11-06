"""Compatibility imports for shared service configuration."""

from palinreplay.restoration.service_config import (
    ServiceInput,
    ServicesConfig,
    ServiceSpec,
    load_service_input,
    load_services,
)
from palinreplay.restoration.service_config import (
    resolve_services as _resolve_services,
)

__all__ = [
    "ServiceInput",
    "ServiceSpec",
    "ServicesConfig",
    "load_service_input",
    "load_services",
    "resolve_services",
]


def resolve_services(service_input, store, plan):
    """Retain the existing Replay-plan interface for external callers."""
    return _resolve_services(service_input, store, plan.start, end_index=plan.end.index)
