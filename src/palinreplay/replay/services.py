"""Run explicitly configured services through a restored container executor."""

import base64
import time
from dataclasses import dataclass
from pathlib import Path

from palinreplay.replay.service_config import ServicesConfig, ServiceSpec
from palinreplay.storage.io import write_json


class PreparationError(RuntimeError):
    """A configured service could not be prepared; historical tools must not run."""


class PreparationJournal:
    """Durable evidence, including failures before a recording/C0 exists."""

    def __init__(self, path: Path, config: ServicesConfig):
        self.path = Path(path)
        self.data = {"status": "preparing", "config": config.model_dump(mode="json"), "services": []}
        write_json(self.path, self.data)

    def save(self):
        write_json(self.path, self.data, replace=True)

    def fail(self, entry, error):
        entry.update(status="failed", error_type=type(error).__name__, error=str(error))
        self.data["status"] = "failed"
        self.save()


@dataclass
class StartedService:
    spec: ServiceSpec
    deadline: float
    entry: dict


def _command(executor, service, command, deadline, attempt, journal):
    remaining = deadline - time.monotonic()
    attempt.update(command=command, cwd=service.cwd or executor.runtime.launch.workdir,
                   environment_overrides=dict(service.env), timeout_sec=remaining, status="running")
    journal.save()
    started = time.monotonic()
    try:
        if remaining <= 0:
            raise PreparationError(f"{service.name}: service deadline exceeded")
        result = executor.exec(command, cwd=service.cwd, env=service.env, timeout_sec=remaining)
        attempt.update(status="returned", exit_code=result.exit_code,
                       output_base64=base64.b64encode(result.output).decode("ascii"),
                       duration_sec=result.duration_sec)
        journal.save()
        return result
    except BaseException as error:
        attempt.update(status="unknown", error_type=type(error).__name__, error=str(error),
                       duration_sec=time.monotonic() - started)
        journal.save()
        raise


def start_service(executor, service: ServiceSpec, journal: PreparationJournal) -> StartedService:
    """Execute the supplied startup exactly once; the command owns daemonization."""
    entry = {"name": service.name, "status": "starting", "startup": {}}
    journal.data["services"].append(entry)
    started = StartedService(service, time.monotonic() + service.timeout_sec, entry)
    try:
        result = _command(executor, service, service.start_command, started.deadline,
                          entry["startup"], journal)
        if result.exit_code != 0:
            raise PreparationError(f"{service.name}: startup exited with code {result.exit_code}")
        entry["status"] = "started"
        journal.save()
        return started
    except BaseException as error:
        journal.fail(entry, error)
        raise
