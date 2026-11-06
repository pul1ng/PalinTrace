"""Compatibility imports for shared service configuration."""

from palinreplay.restoration.service_config import (
    ServiceInput,
    ServicesConfig,
    ServiceSpec,
    load_service_input,
    load_services,
    resolve_services,
)

__all__ = [
    "ServiceInput",
    "ServiceSpec",
    "ServicesConfig",
    "load_service_input",
    "load_services",
    "resolve_services",
]
