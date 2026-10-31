"""Launch metadata and stable-source checks for Btrfs C0 freezing."""


from palinreplay.preload.environment import OWNER_LABEL
from palinreplay.sandbox.base import EnvironmentMismatch
from palinreplay.sandbox.runtime import C0LaunchConfig, _environment, _runtime


def _snapshot(environment, inputs, public_env_keys, owner, *, stopped=False):
    container = environment.container
    runtime = _runtime(container.client, container, frozenset(public_env_keys))
    attrs, config = container.attrs, container.attrs.get("Config") or {}
    labels = config.get("Labels") or {}
    if (container.id != environment.container_id or runtime.source_image_id != environment.image_id
            or runtime.platform != inputs.platform or labels.get(OWNER_LABEL) != owner
            or labels.get("palinreplay.preload.task") != inputs.task_id
            or runtime.workdir != inputs.workdir):
        raise EnvironmentMismatch("C0 source identity or launch configuration changed")
    actual_env = _environment(config)
    if any(actual_env.get(key) != value for key, value in inputs.env.items()):
        raise EnvironmentMismatch("C0 source environment differs from initialization input")
    entrypoint, command = tuple(config.get("Entrypoint") or ()), tuple(config.get("Cmd") or ())
    if entrypoint != ("sleep",) or command != ("infinity",):
        raise EnvironmentMismatch("C0 freeze requires the controlled idle container entrypoint")
    state = attrs.get("State") or {}
    if stopped and (state.get("Status") != "exited" or state.get("Running")
                    or state.get("Paused") or state.get("Restarting")):
        raise EnvironmentMismatch("C0 container must remain stopped during capture")
    fields = runtime.model_dump(exclude={"schema_version", "source_image_id", "source_container_id"})
    launch = C0LaunchConfig(**fields, shell=inputs.shell, entrypoint=entrypoint, command=command)
    stamp = tuple(state.get(key) for key in ("StartedAt", "FinishedAt", "Pid", "ExitCode"))
    return launch, (*stamp, attrs.get("RestartCount"))
