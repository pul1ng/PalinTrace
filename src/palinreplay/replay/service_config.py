"""Explicit service definitions; never infer services from recorded history."""

from pathlib import Path

from pydantic import Field, StrictStr, field_validator

from palinreplay.core.schema import Spec


class ServiceSpec(Spec):
    name: StrictStr
    start_command: StrictStr
    readiness_command: StrictStr
    timeout_sec: float = Field(strict=True, gt=0, allow_inf_nan=False)
    cwd: StrictStr | None = None
    env: dict[StrictStr, StrictStr] = Field(default_factory=dict)

    @field_validator("name", "start_command", "readiness_command")
    @classmethod
    def nonempty(cls, value):
        if not value.strip() or "\x00" in value:
            raise ValueError("service names and commands must be nonempty and contain no NUL")
        return value

    @field_validator("cwd")
    @classmethod
    def workdir(cls, value):
        if value is not None and (not value.startswith("/") or "\x00" in value):
            raise ValueError("cwd must be an absolute container path without NUL")
        return value

    @field_validator("env")
    @classmethod
    def environment(cls, value):
        if any(not key or "=" in key or "\x00" in key + content
               for key, content in value.items()):
            raise ValueError("env must contain valid environment keys and values")
        return value


class ServicesConfig(Spec):
    services: tuple[ServiceSpec, ...]

    @field_validator("services")
    @classmethod
    def unique_names(cls, value):
        if len({service.name for service in value}) != len(value):
            raise ValueError("service names must be unique")
        return value


def load_services(path: str | Path) -> ServicesConfig:
    """Validate a file without opening a recording or creating runtime resources."""
    return ServicesConfig.model_validate_json(Path(path).read_bytes())
