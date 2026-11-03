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

    def save(self, *, primary=None):
        try:
            write_json(self.path, self.data, replace=True)
        except BaseException as error:
            if primary is None:
                raise
            detail = f"{type(error).__name__}: {error}"
            self.data.setdefault("evidence_errors", []).append(detail)
            primary.add_note(f"preparation evidence could not be saved: {detail}")

    def fail(self, entry, error):
        entry.update(status="failed", error_type=type(error).__name__, error=str(error))
        self.data["status"] = "failed"
        self.save(primary=error)


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
    except BaseException as error:
        attempt.update(status="unknown", error_type=type(error).__name__, error=str(error),
                       duration_sec=time.monotonic() - started)
        journal.save(primary=error)
        raise
    attempt.update(status="returned", exit_code=result.exit_code,
                   output_base64=base64.b64encode(result.output).decode("ascii"),
                   duration_sec=result.duration_sec)
    journal.save()
    return result


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


def prepare_services(executor, config: ServicesConfig, journal: PreparationJournal):
    """Prepare declared services in order, then confirm each before publishing ready."""
    started_services = []
    for service in config.services:
        started = start_service(executor, service, journal)
        wait_ready(executor, started, journal)
        started_services.append(started)
    for started in started_services:
        _confirm_ready(executor, started, journal)
    journal.data["status"] = "ready"
    journal.save()
    return journal.data


def _confirm_ready(executor, started: StartedService, journal: PreparationJournal):
    """Recheck once with a fresh per-command budget, retaining the initial probes."""
    service, entry = started.spec, started.entry
    entry.update(status="confirming", final_readiness={})
    deadline = time.monotonic() + service.timeout_sec
    try:
        result = _command(executor, service, service.readiness_command, deadline,
                          entry["final_readiness"], journal)
        if result.exit_code != 0:
            raise PreparationError(f"{service.name}: final readiness exited with code {result.exit_code}")
        if time.monotonic() > deadline:
            raise PreparationError(f"{service.name}: final readiness deadline exceeded")
        entry["status"] = "ready"
        journal.save()
    except BaseException as error:
        journal.fail(entry, error)
        raise


def wait_ready(executor, started: StartedService, journal: PreparationJournal):
    """Poll only the supplied readiness command within the startup deadline."""
    service, entry = started.spec, started.entry
    entry.update(status="checking", readiness=[])
    journal.save()
    try:
        while time.monotonic() < started.deadline:
            attempt = {}
            entry["readiness"].append(attempt)
            result = _command(executor, service, service.readiness_command, started.deadline,
                              attempt, journal)
            if result.exit_code == 0 and time.monotonic() <= started.deadline:
                entry["status"] = "ready"
                journal.save()
                return entry
            remaining = started.deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(0.1, remaining))
        raise PreparationError(f"{service.name}: readiness deadline exceeded")
    except BaseException as error:
        journal.fail(entry, error)
        raise
