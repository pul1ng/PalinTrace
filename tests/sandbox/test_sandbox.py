import io
import json
import shlex
import sys
import tarfile
import threading
from types import SimpleNamespace

import pytest

from palinreplay.sandbox import (
    DockerSDKSandbox,
    EnvironmentMismatch,
    SandboxCleanupError,
    SandboxError,
    SandboxExecutionError,
    SandboxTimeout,
    load_sandbox_task,
)
from palinreplay.sandbox import client as connection
from palinreplay.sandbox.docker import OWNER_LABEL
from palinreplay.tasks import convert
from palinreplay.tasks.images import PreparedImage, PreparedImages
from palinreplay.tasks.model import file_sha256

IMAGE_ID = "sha256:" + "b" * 64


@pytest.fixture
def package(tmp_path, row):
    source = tmp_path / "rows.json"
    source.write_text(json.dumps([row]))
    root = tmp_path / "tasks"
    convert(root, input_path=source, source="swebench_lite")
    reference = "docker.io/swebench/sweb.eval.x86_64.example_1776_repo-1:latest"
    prepared = PreparedImages(
        dataset_manifest_sha256=file_sha256(root / "manifest.json"),
        images=[
            PreparedImage(
                reference=reference,
                platform="linux/amd64",
                image_id=IMAGE_ID,
                repo_digests=[],
                task_ids=[row["instance_id"]],
            )
        ],
    )
    (root / "images.json").write_text(prepared.model_dump_json())
    return root


class Missing(Exception):
    status_code = 404


class FakeContainer:
    def __init__(self, kwargs):
        self.id = "container-" + kwargs["name"]
        self.kwargs = kwargs
        self.attrs = {
            "Name": "/" + kwargs["name"],
            "Image": IMAGE_ID,
            "State": {"Status": "created"},
            "Mounts": [],
            "Config": {
                "Image": kwargs["image"],
                "WorkingDir": kwargs["working_dir"],
                "Labels": dict(kwargs["labels"]),
                "Env": ["HOME=/root", *[f"{k}={v}" for k, v in kwargs["environment"].items()]],
            },
            "HostConfig": {
                "NetworkMode": kwargs["network_mode"],
                "Memory": kwargs["mem_limit"],
                "NanoCpus": kwargs["nano_cpus"],
            },
        }
        self.removed = False
        self.calls = []
        self.removals = []
        self.responses = {}
        self.image_head = "a" * 40
        self.runtime_head = None
        self.archive_calls = []
        self.start_error = False
        self.remove_error = False
        self.stopped = threading.Event()
        self.started_exec = threading.Event()

    def get_archive(self, path):
        assert self.attrs["State"]["Status"] == "created", "must read before any process runs"
        assert not self.calls, "must read before BASH_ENV or other startup commands"
        self.archive_calls.append(path)
        if path == "/testbed/.git/HEAD":
            content = b"ref: refs/heads/main\n"
        elif path == "/testbed/.git/refs/heads/main":
            content = (self.image_head + "\n").encode()
        else:
            raise Missing(path)
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode="w") as archive:
            entry = tarfile.TarInfo(path.rsplit("/", 1)[-1])
            entry.size = len(content)
            archive.addfile(entry, io.BytesIO(content))
        return iter([data.getvalue()]), {}

    def start(self):
        if self.start_error:
            raise RuntimeError("start failed")
        self.attrs["State"]["Status"] = "running"

    def reload(self):
        if self.removed:
            raise Missing()

    def exec_run(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        command = argv[-1]
        if command == "hang":
            self.started_exec.set()
            assert self.stopped.wait(5), "test worker must be unblocked by cleanup"
            return SimpleNamespace(exit_code=137, output=b"late")
        if command == "transport-error":
            raise OSError("lost response")
        if command in self.responses:
            return self.responses[command]
        if command == "pwd -P":
            text = "/testbed\n"
        elif command == "git rev-parse HEAD":
            text = (self.runtime_head or self.image_head) + "\n"
        elif command.startswith("git rev-parse --verify"):
            text = ("d" if "^{tree}" in command else "a") * 40 + "\n"
        elif command.startswith("git rev-list --count"):
            text = "0\n" if self.image_head == "a" * 40 else "1\n"
        elif command.startswith(("git merge-base", "git diff --raw", "git -c core.fileMode")):
            text = ""
        elif command.startswith("test -r"):
            text = "/root/.bashrc\n"
        elif command.startswith("python -c"):
            text = json.dumps(
                {
                    "conda_env": "testbed",
                    "conda_prefix": "/opt/miniconda3/envs/testbed",
                    "prefix": "/opt/miniconda3/envs/testbed",
                    "executable": "/opt/miniconda3/envs/testbed/bin/python",
                }
            )
        else:
            text = "stdout\nstderr\n"
        return SimpleNamespace(exit_code=0, output=text.encode())

    def remove(self, **kwargs):
        self.removals.append(kwargs)
        if self.remove_error:
            raise OSError("daemon offline")
        self.removed = True
        self.stopped.set()


class FakeClient:
    def __init__(self):
        self.create_calls = []
        self.get_calls = []
        self.image_gets = []
        self.objects = []
        self.image = SimpleNamespace(
            id=IMAGE_ID, attrs={"Os": "linux", "Architecture": "amd64", "Config": {}}
        )
        self.images = SimpleNamespace(get=self.get_image)
        self.containers = SimpleNamespace(create=self.create, get=self.get)
        self.on_create = lambda c: None
        self.create_error = False
        self.closed = False

    def info(self):
        return {"OSType": "linux", "Architecture": "aarch64"}

    def get_image(self, image_id):
        self.image_gets.append(image_id)
        return self.image

    def create(self, **kwargs):
        self.create_calls.append(kwargs)
        container = FakeContainer(kwargs)
        self.objects.append(container)
        self.on_create(container)
        if self.create_error:
            raise OSError("create response lost")
        return container

    def get(self, identifier):
        self.get_calls.append(identifier)
        for container in self.objects:
            if identifier in (container.id, container.kwargs["name"]) and not container.removed:
                return container
        raise Missing()

    def close(self):
        self.closed = True


@pytest.fixture
def client():
    return FakeClient()


def test_binding_matches_task_and_prepared_manifest(package):
    bound = load_sandbox_task(package)
    assert bound.task.spec.task_id == "example__repo-1"
    assert bound.image.image_id == IMAGE_ID
    assert bound.dataset_manifest_sha256 == file_sha256(package / "manifest.json")
    assert bound.images_manifest_sha256 == file_sha256(package / "images.json")


@pytest.mark.parametrize(
    "damage", ["manifest", "reference", "platform", "unknown", "duplicate", "missing", "empty"]
)
def test_rejects_bad_image_bindings_before_docker(package, damage):
    path = package / "images.json"
    data = json.loads(path.read_text())
    if damage == "manifest":
        data["dataset_manifest_sha256"] = "c" * 64
    elif damage == "reference":
        data["images"][0]["reference"] = "another/image:tag"
    elif damage == "platform":
        data["images"][0]["platform"] = "linux/arm64"
    elif damage == "unknown":
        data["images"][0]["task_ids"] = ["unknown"]
    elif damage == "duplicate":
        data["images"].append(data["images"][0])
    elif damage == "missing":
        data["images"] = []
    else:
        data["images"][0]["task_ids"] = []
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        DockerSDKSandbox.from_task_package(package)


def test_missing_images_and_unknown_task(package):
    with pytest.raises(ValueError, match="unknown task"):
        load_sandbox_task(package, "absent")
    (package / "images.json").unlink()
    with pytest.raises(ValueError, match="download mirror"):
        load_sandbox_task(package)


def test_corrupt_task_rejected(package):
    (package / "example__repo-1" / "instruction.md").write_text("changed")
    with pytest.raises(ValueError, match="corrupt"):
        load_sandbox_task(package)


def test_start_exec_inspect_and_cleanup(package, client):
    sandbox = DockerSDKSandbox.from_task_package(package, client=client, network_mode="none")
    with sandbox:
        info = sandbox.inspect()
        assert sandbox.checks.base_commit == "a" * 40
        assert sandbox.checks.initialization == info.initialization == "preserve-image"
        assert sandbox.checks.repository.image_head == "a" * 40
        assert sandbox.checks.repository.head == "a" * 40
        assert sandbox.checks.repository.relation == "same"
        assert sandbox.checks.repository.commits_ahead == 0
        assert sandbox.checks.repository.tracked_worktree_clean
        assert sandbox.checks.bash_env == "/root/.bashrc"
        assert sandbox.checks.conda_env == "testbed"
        assert info.image_id == IMAGE_ID
        assert info.daemon_platform == "linux/arm64"
        assert info.platform == "linux/amd64"
        assert info.network_mode == "none" and not info.mounts
        assert info.memory_bytes == 2 * 1024**3 and info.nano_cpus == 2_000_000_000
        result = sandbox.exec("printf hi", cwd="/tmp", env={"LOCAL_ONLY": "x"})
        assert result.output == b"stdout\nstderr\n" and result.exit_code == 0
        assert result.duration_sec >= 0
        sandbox.exec("second command")
        container = client.objects[0]
        argv, kwargs = container.calls[-2]
        assert argv == ["bash", "-c", "printf hi"]
        assert kwargs["workdir"] == "/tmp"
        assert kwargs["environment"]["BASH_ENV"] == "/root/.bashrc"
        assert kwargs["environment"]["LOCAL_ONLY"] == "x"
        assert not kwargs["demux"] and not kwargs["tty"] and not kwargs["stream"]
        assert "LOCAL_ONLY" not in container.calls[-1][1]["environment"]
        assert container.calls[-1][1]["workdir"] == "/testbed"
        assert client.image_gets == [IMAGE_ID]
        assert container.archive_calls == [
            "/testbed/.git/HEAD", "/testbed/.git/refs/heads/main"
        ]
        assert len(client.create_calls) == 1
        create = client.create_calls[0]
        assert create["image"] == IMAGE_ID
        assert create["entrypoint"] == ["sleep"] and create["command"] == ["infinity"]
        assert create["volumes"] == {} and create["ports"] == {}
        assert not create["privileged"] and not create["auto_remove"]
        assert str(package) not in str(create)
    assert sandbox.state == "closed"
    assert container.removals == [{"force": True, "v": True}]
    assert sandbox.last_container_id == container.id
    assert not client.closed  # injected client is caller-owned
    sandbox.close()
    assert len(container.removals) == 1
    with pytest.raises(SandboxError, match="not running"):
        sandbox.exec("after close")
    with pytest.raises(SandboxError, match="single-use"):
        sandbox.start()


def test_tool_nonzero_and_raw_bytes_are_not_infrastructure_errors(package, client):
    with DockerSDKSandbox.from_task_package(package, client=client) as sandbox:
        client.objects[0].responses["exit 7"] = SimpleNamespace(exit_code=7, output=b"bad\xff\n")
        result = sandbox.exec("exit 7")
        assert result.exit_code == 7 and result.output == b"bad\xff\n"
        assert "\ufffd" in result.text
        assert sandbox.state == "running"
        assert sandbox.exec("next").exit_code == 0


@pytest.mark.parametrize("probe,value", [("pwd -P", "/wrong"), ("git rev-parse HEAD", "b" * 40)])
def test_mismatched_environment_aborts_and_removes(package, client, probe, value):
    client.on_create = lambda c: c.responses.update(
        {probe: SimpleNamespace(exit_code=0, output=value.encode())}
    )
    sandbox = DockerSDKSandbox.from_task_package(package, client=client)
    with pytest.raises(EnvironmentMismatch, match="mismatch"):
        sandbox.start()
    assert sandbox.state == "closed" and client.objects[0].removed


def test_conda_must_be_active(package, client):
    original = FakeContainer.exec_run

    def inactive(self, argv, **kwargs):
        if argv[-1].startswith("python -c"):
            return SimpleNamespace(
                exit_code=0,
                output=b'{"conda_env":"base","prefix":"/base","conda_prefix":"/base","executable":"/base/python"}',
            )
        return original(self, argv, **kwargs)

    client.on_create = lambda c: setattr(c, "exec_run", inactive.__get__(c))
    with pytest.raises(EnvironmentMismatch, match="testbed conda"):
        DockerSDKSandbox.from_task_package(package, client=client).start()
    assert client.objects[0].removed


@pytest.mark.parametrize("damage", ["id", "platform", "volumes"])
def test_image_preflight_before_create(package, client, damage):
    if damage == "id":
        client.image.id = "sha256:" + "c" * 64
    elif damage == "platform":
        client.image.attrs["Architecture"] = "arm64"
    else:
        client.image.attrs["Config"]["Volumes"] = {"/data": {}}
    with pytest.raises(EnvironmentMismatch):
        DockerSDKSandbox.from_task_package(package, client=client).start()
    assert client.create_calls == []


def test_start_failure_cleans_up(package, client):
    client.on_create = lambda c: setattr(c, "start_error", True)
    with pytest.raises(SandboxError, match="startup failed"):
        DockerSDKSandbox.from_task_package(package, client=client).start()
    assert client.objects[0].removed


def test_create_response_loss_cleans_only_owned_container(package, client):
    client.create_error = True
    sandbox = DockerSDKSandbox.from_task_package(package, client=client)
    with pytest.raises(SandboxError, match="create response lost"):
        sandbox.start()
    assert client.objects[0].removed
    assert sandbox.name in client.get_calls


def test_foreign_owner_never_removed(package, client):
    client.create_error = True
    client.on_create = lambda c: c.attrs["Config"]["Labels"].update({OWNER_LABEL: "someone-else"})
    with pytest.raises(SandboxCleanupError, match="do not own"):
        DockerSDKSandbox.from_task_package(package, client=client).start()
    assert not client.objects[0].removed and not client.objects[0].removals


def test_timeout_discards_container_and_does_not_retry(package, client):
    sandbox = DockerSDKSandbox.from_task_package(package, client=client)
    sandbox.start()
    with pytest.raises(SandboxTimeout, match="no retry"):
        sandbox.exec("hang", timeout_sec=0.05)
    container = client.objects[0]
    assert container.started_exec.is_set() and container.removed
    assert sandbox.state == "closed"
    assert sum(argv[-1] == "hang" for argv, _ in container.calls) == 1
    with pytest.raises(SandboxError):
        sandbox.exec("next")


def test_transport_error_is_unknown_not_retried(package, client):
    sandbox = DockerSDKSandbox.from_task_package(package, client=client)
    sandbox.start()
    with pytest.raises(SandboxExecutionError, match="outcome unknown"):
        sandbox.exec("transport-error")
    assert client.objects[0].removed
    assert sum(argv[-1] == "transport-error" for argv, _ in client.objects[0].calls) == 1


def test_cleanup_failure_is_visible_and_retryable(package, client):
    sandbox = DockerSDKSandbox.from_task_package(package, client=client)
    sandbox.start()
    container = client.objects[0]
    container.remove_error = True
    with pytest.raises(SandboxCleanupError, match=container.id):
        sandbox.close()
    assert sandbox.state == "failed" and not container.removed
    container.remove_error = False
    sandbox.close()
    assert sandbox.state == "closed" and container.removed


def test_context_manager_cleans_on_caller_exception(package, client):
    with (
        pytest.raises(ValueError, match="caller failure"),
        DockerSDKSandbox.from_task_package(package, client=client),
    ):
        raise ValueError("caller failure")
    assert client.objects[0].removed


def test_two_sandboxes_do_not_share_lifetime(package, client):
    first = DockerSDKSandbox.from_task_package(package, client=client)
    second = DockerSDKSandbox.from_task_package(package, client=client)
    with first, second:
        assert first.name != second.name
        first.close()
        assert second.exec("still running").exit_code == 0
        assert not client.objects[1].removed
    assert all(c.removed for c in client.objects)


@pytest.mark.parametrize(
    "options",
    [
        {"cpus": 0},
        {"cpus": float("nan")},
        {"memory_bytes": 0},
        {"memory_bytes": True},
        {"api_timeout_sec": float("inf")},
        {"network_mode": "host"},
    ],
)
def test_invalid_limits_before_docker(package, client, options):
    with pytest.raises(ValueError):
        DockerSDKSandbox.from_task_package(package, client=client, **options)
    assert not client.create_calls


@pytest.mark.parametrize(
    "options",
    [
        {"timeout_sec": 0},
        {"timeout_sec": float("nan")},
        {"cwd": "relative"},
        {"env": {"BAD=KEY": "value"}},
    ],
)
def test_invalid_exec_options_do_not_execute(package, client, options):
    with DockerSDKSandbox.from_task_package(package, client=client) as sandbox:
        count = len(client.objects[0].calls)
        with pytest.raises(ValueError):
            sandbox.exec("not executed", **options)
        assert len(client.objects[0].calls) == count


@pytest.mark.parametrize("command", ["", " \n"])
def test_empty_shell_commands_are_forwarded_verbatim(package, client, command):
    with DockerSDKSandbox.from_task_package(package, client=client) as sandbox:
        client.objects[0].responses[command] = SimpleNamespace(exit_code=0, output=b"")
        result = sandbox.exec(command)
        assert result.command == command and result.output == b"" and result.exit_code == 0
        assert client.objects[0].calls[-1][0][-1] == command


def test_cli_context_resolution_and_precedence(monkeypatch):
    calls = []

    def context(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(
            stdout=json.dumps([{"Endpoints": {"docker": {"Host": "unix:///orbstack.sock"}}}])
        )

    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.delenv("DOCKER_CONTEXT", raising=False)
    monkeypatch.setattr(connection.subprocess, "run", context)
    assert connection.docker_endpoint() == "unix:///orbstack.sock"
    assert calls == [["docker", "context", "inspect"]]
    monkeypatch.setenv("DOCKER_HOST", "unix:///explicit.sock")
    assert connection.docker_endpoint() == "unix:///explicit.sock"
    assert len(calls) == 1
    monkeypatch.setenv("DOCKER_CONTEXT", "orbstack")
    assert connection.docker_endpoint() == "unix:///orbstack.sock"
    assert calls[-1] == ["docker", "context", "inspect", "orbstack"]


def test_nonlocal_endpoint_is_explicitly_unsupported(monkeypatch):
    monkeypatch.delenv("DOCKER_CONTEXT", raising=False)
    monkeypatch.setenv("DOCKER_HOST", "tcp://example:2375")
    with pytest.raises(SandboxError, match="local Unix"):
        connection.docker_endpoint()


def test_optional_sdk_dependency_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "docker", None)
    with pytest.raises(SandboxError, match=r"palinreplay\[docker\]"):
        connection.connect_docker(15)


def test_multi_task_package_requires_explicit_selection(tmp_path, row):
    source = tmp_path / "rows.json"
    source.write_text(json.dumps([row, row | {"instance_id": "example__repo-2"}]))
    root = tmp_path / "tasks"
    manifest = convert(root, input_path=source, source="swebench_lite")
    entries = []
    for task in manifest.tasks:
        spec = json.loads((root / task.task_id / "task.json").read_text())
        entries.append(
            PreparedImage(
                reference=spec["environment"]["image"]["reference"],
                platform="linux/amd64",
                image_id=IMAGE_ID,
                repo_digests=[],
                task_ids=[task.task_id],
            )
        )
    (root / "images.json").write_text(
        PreparedImages(
            dataset_manifest_sha256=file_sha256(root / "manifest.json"), images=entries
        ).model_dump_json()
    )
    with pytest.raises(ValueError, match="task_id is required"):
        load_sandbox_task(root)
    assert load_sandbox_task(root, "example__repo-2").task.spec.task_id == "example__repo-2"


def test_missing_local_image_never_creates_or_pulls(package, client):
    def missing(image_id):
        raise Missing("local image not found")

    client.images.get = missing
    with pytest.raises(SandboxError, match="local image not found"):
        DockerSDKSandbox.from_task_package(package, client=client).start()
    assert not client.create_calls


@pytest.mark.parametrize("damage", ["image", "mounts", "environment"])
def test_unexpected_actual_container_config_aborts(package, client, damage):
    def change(container):
        if damage == "image":
            container.attrs["Config"]["Image"] = "mutable:tag"
        elif damage == "mounts":
            container.attrs["Mounts"] = [{"Destination": "/unexpected"}]
        else:
            container.attrs["Config"]["Env"] = []

    client.on_create = change
    with pytest.raises(EnvironmentMismatch):
        DockerSDKSandbox.from_task_package(package, client=client).start()
    assert client.objects[0].removed


def test_unknown_exit_code_discards_container(package, client):
    sandbox = DockerSDKSandbox.from_task_package(package, client=client)
    sandbox.start()
    client.objects[0].responses["unknown"] = SimpleNamespace(exit_code=None, output=b"partial")
    with pytest.raises(SandboxExecutionError, match="no definitive"):
        sandbox.exec("unknown")
    assert client.objects[0].removed


def test_already_removed_container_cleanup_is_idempotent(package, client):
    sandbox = DockerSDKSandbox.from_task_package(package, client=client)
    sandbox.start()
    client.objects[0].removed = True
    sandbox.close()
    sandbox.close()
    assert sandbox.state == "closed"


def test_tiny_cpu_quota_rejected(package, client):
    with pytest.raises(ValueError, match="nanocpu"):
        DockerSDKSandbox.from_task_package(package, client=client, cpus=1e-12)


def test_bad_context_response_is_actionable(monkeypatch):
    monkeypatch.delenv("DOCKER_CONTEXT", raising=False)
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.setattr(connection.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout="[]"))
    with pytest.raises(SandboxError, match="context inspect"):
        connection.docker_endpoint()


def test_startup_exec_failure_does_not_reopen_closed_client(package, monkeypatch, client):
    monkeypatch.setattr("palinreplay.sandbox.docker.connect_docker", lambda timeout: client)
    client.on_create = lambda c: c.responses.update(
        {"pwd -P": SimpleNamespace(exit_code=None, output=b"unknown")}
    )
    sandbox = DockerSDKSandbox.from_task_package(package)
    with pytest.raises(SandboxExecutionError, match="no definitive"):
        sandbox.start()
    assert sandbox.state == "closed" and client.closed
    assert len(client.objects[0].removals) == 1
    sandbox.close()


def test_owned_client_is_closed(package, monkeypatch, client):
    monkeypatch.setattr("palinreplay.sandbox.docker.connect_docker", lambda timeout: client)
    with DockerSDKSandbox.from_task_package(package):
        pass
    assert client.closed


def test_preserves_prepared_commit_not_task_base(package, client):
    def prepared(container):
        container.image_head = "c" * 40

    client.on_create = prepared
    before = {p: p.read_bytes() for p in package.rglob("*") if p.is_file()}
    with DockerSDKSandbox.from_task_package(package, client=client) as sandbox:
        repo = sandbox.checks.repository
        assert sandbox.checks.initialization == "preserve-image"
        assert sandbox.checks.base_commit == repo.base_commit == "a" * 40
        assert repo.image_head == repo.head == "c" * 40
        assert repo.relation == "descendant" and repo.commits_ahead == 1
        for argv, options in client.objects[0].calls:
            words = shlex.split(argv[-1])
            assert not {"checkout", "reset", "clean", "fetch", "clone", "apply"} & set(words)
            if words[0] == "git":
                assert options["environment"]["GIT_OPTIONAL_LOCKS"] == "0"
                assert options["environment"]["GIT_NO_REPLACE_OBJECTS"] == "1"
    assert {p: p.read_bytes() for p in before} == before
    assert client.objects[0].removed


@pytest.mark.parametrize("runtime", ["a" * 40, "e" * 40])
def test_startup_must_preserve_image_head_even_if_runtime_is_task_base(package, client, runtime):
    def changed(container):
        container.image_head = "c" * 40
        container.runtime_head = runtime

    client.on_create = changed
    with pytest.raises(EnvironmentMismatch, match="prepared image HEAD mismatch"):
        DockerSDKSandbox.from_task_package(package, client=client).start()
    assert client.objects[0].removed


@pytest.mark.parametrize("failure", ["missing-base", "unrelated", "dirty", "count", "diff"])
def test_invalid_prepared_repository_is_not_accepted(package, client, failure):
    base, head = "a" * 40, "c" * 40
    commands = {
        "missing-base": ["git", "rev-parse", "--verify", "--end-of-options", f"{base}^{{commit}}"],
        "unrelated": ["git", "merge-base", "--is-ancestor", base, head],
        "dirty": [
            "git", "-c", "core.fileMode=true", "diff", "--no-ext-diff", "--no-textconv",
            "--quiet", "HEAD", "--",
        ],
        "count": ["git", "rev-list", "--count", f"{base}..{head}"],
        "diff": [
            "git", "diff", "--raw", "-z", "--no-abbrev", "--no-renames", "--no-relative",
            "--no-ext-diff", "--no-textconv", base, head, "--",
        ],
    }

    def invalid(container):
        container.image_head = head
        container.responses[shlex.join(commands[failure])] = SimpleNamespace(
            exit_code=0 if failure in {"count", "diff"} else 1, output=b"invalid"
        )

    client.on_create = invalid
    with pytest.raises(EnvironmentMismatch):
        DockerSDKSandbox.from_task_package(package, client=client).start()
    assert client.objects[0].removed


def test_unreadable_image_head_stops_before_process_start(package, client):
    def missing(path):
        raise Missing(path)

    client.on_create = lambda c: setattr(c, "get_archive", missing)
    with pytest.raises(EnvironmentMismatch, match="cannot read prepared image Git HEAD"):
        DockerSDKSandbox.from_task_package(package, client=client).start()
    container = client.objects[0]
    assert container.removed and not container.calls
    assert container.attrs["State"]["Status"] == "created"


def test_head_change_during_verification_is_rejected(package, client):
    original = FakeContainer.exec_run

    def moving(self, argv, **kwargs):
        if argv[-1] == "git rev-parse HEAD" and any(
            call[0][-1] == "git rev-parse HEAD" for call in self.calls
        ):
            return SimpleNamespace(exit_code=0, output=("c" * 40).encode())
        return original(self, argv, **kwargs)

    client.on_create = lambda c: setattr(c, "exec_run", moving.__get__(c))
    with pytest.raises(EnvironmentMismatch, match="changed during startup"):
        DockerSDKSandbox.from_task_package(package, client=client).start()
    assert client.objects[0].removed
