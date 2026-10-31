"""Connect to the same local daemon selected by the Docker CLI (including OrbStack)."""

import json
import os
import subprocess

from palinreplay.sandbox.base import SandboxError


def docker_endpoint() -> str:
    # Like Docker CLI: explicit context wins over DOCKER_HOST; otherwise host wins.
    context = os.environ.get("DOCKER_CONTEXT")
    host = os.environ.get("DOCKER_HOST")
    if not context and host:
        endpoint = host
    else:
        command = ["docker", "context", "inspect"]
        if context:
            command.append(context)
        try:
            result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=10)
            contexts = json.loads(result.stdout)
            if not isinstance(contexts, list) or len(contexts) != 1:
                raise ValueError("expected one context")
            endpoint = contexts[0]["Endpoints"]["docker"]["Host"]
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as exc:
            raise SandboxError(
                "cannot resolve Docker context; check docker context inspect"
            ) from exc
    if not isinstance(endpoint, str) or not endpoint.startswith("unix:///"):
        raise SandboxError("first-release Sandbox supports local Unix Docker sockets only")
    return endpoint


def connect_docker(api_timeout_sec: float):
    try:
        import docker
    except ImportError as exc:
        raise SandboxError('Docker SDK requires: pip install "palinreplay[docker]"') from exc
    endpoint = docker_endpoint()
    try:
        return docker.DockerClient(base_url=endpoint, version="auto", timeout=api_timeout_sec)
    except docker.errors.DockerException as exc:
        raise SandboxError(
            "cannot connect to Docker; start the daemon and check docker info"
        ) from exc
