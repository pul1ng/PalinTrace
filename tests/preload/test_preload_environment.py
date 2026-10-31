from types import SimpleNamespace

import pytest

import palinreplay.preload.environment as preload_environment
from palinreplay.preload.environment import (
    OWNER_LABEL,
    PreloadCleanupError,
    create_preload_environment,
)
from palinreplay.preload.input import PreloadInput
from palinreplay.sandbox.base import EnvironmentMismatch, SandboxCleanupError, SandboxError

IMAGE_ID = "sha256:" + "a" * 64
OTHER_ID = "sha256:" + "b" * 64
DIGEST = "registry.example/task@sha256:" + "c" * 64


class APIError(Exception):
    def __init__(self, status_code):
        self.status_code = status_code
        super().__init__(str(status_code))


class Container:
    def __init__(self, client, options):
        self.client, self.options = client, options
        self.id = "container-" + options["name"]
        self.removed = False
        self.attrs = {
            "Image": options["image"], "State": {"Status": "created"}, "Mounts": [],
            "Config": {
                "Image": options["image"], "Labels": dict(options["labels"]),
                "WorkingDir": options["working_dir"],
                "Env": ["PATH=/usr/bin", *[f"{k}={v}" for k, v in options["environment"].items()]],
            },
        }

    def reload(self):
        if self.removed:
            raise APIError(404)

    def remove(self, **kwargs):
        assert kwargs == {"force": True, "v": True}
        if self.client.remove_error:
            raise OSError("removal response lost")
        if not self.client.remove_noop:
            self.removed = True


class Client:
    def __init__(self):
        self.image = SimpleNamespace(
            id=IMAGE_ID, attrs={"Os": "linux", "Architecture": "amd64", "Config": {}},
        )
        self.images = SimpleNamespace(get=self.get_image, pull=self.pull)
        self.containers = SimpleNamespace(create=self.create, get=self.get_container)
        self.image_gets, self.pulls, self.created = [], [], []
        self.image_missing = self.remove_error = self.remove_noop = False
        self.lost_create = self.foreign = self.close_error = False
        self.inspect_error = self.pull_error = self.create_error = None
        self.os_type = "linux"
        self.on_create = lambda container: None
        self.on_get_image = lambda: None
        self.closed = False

    def info(self):
        return {"OSType": self.os_type}

    def get_image(self, reference):
        self.image_gets.append(reference)
        self.on_get_image()
        if self.inspect_error is not None:
            raise self.inspect_error
        if self.image_missing:
            raise APIError(404)
        return self.image

    def pull(self, reference, **kwargs):
        self.pulls.append((reference, kwargs))
        if self.pull_error is not None:
            raise self.pull_error
        return self.image

    def create(self, **kwargs):
        if self.create_error is not None:
            raise self.create_error
        container = Container(self, kwargs)
        self.created.append(container)
        if self.foreign:
            container.attrs["Config"]["Labels"][OWNER_LABEL] = "another-owner"
        self.on_create(container)
        if self.lost_create:
            raise OSError("create response lost")
        return container

    def get_container(self, identifier):
        for container in self.created:
            if identifier in (container.id, container.options["name"]) and not container.removed:
                return container
        raise APIError(404)

    def close(self):
        self.closed = True
        if self.close_error:
            raise OSError("close failed")


def inputs(**kwargs):
    return PreloadInput(**({
        "image": IMAGE_ID, "platform": "linux/amd64", "instruction": "Never execute this text.",
        "env": {"LANG": "C.UTF-8"}, "workdir": "/testbed", "task_id": "unit-task",
    } | kwargs))


def test_local_image_is_pinned_and_container_is_created_but_not_started():
    client, spec = Client(), inputs()
    with create_preload_environment(spec, client=client) as environment:
        assert client.image_gets == [IMAGE_ID] and client.pulls == []
        assert environment.image_id == IMAGE_ID and environment.pulled_from is None
        assert environment.container_id == environment.container.id
        options = environment.container.options
        assert options["image"] == IMAGE_ID and options["platform"] == "linux/amd64"
        assert options["working_dir"] == spec.workdir and options["environment"] == spec.env
        assert options["entrypoint"] == ["sleep"] and options["command"] == ["infinity"]
        assert not options["privileged"] and options["volumes"] == options["ports"] == {}
        assert not options["use_config_proxy"] and not options["auto_remove"]
        assert "devices" not in options and "cap_add" not in options
        assert spec.instruction not in repr(options)
        assert environment.container.attrs["State"]["Status"] == "created"
        assert not environment.container.removed
    assert environment.container.removed and not client.closed


@pytest.mark.parametrize("image", [IMAGE_ID, DIGEST])
def test_missing_image_pulls_for_requested_platform_and_pins_the_result(image):
    client = Client()
    client.image_missing = True
    with create_preload_environment(
        inputs(image=image, repo_digests=(DIGEST,), image_reference="registry.example/task:v1"),
        client=client,
    ) as environment:
        assert client.pulls == [(DIGEST, {"platform": "linux/amd64"})]
        assert environment.pulled_from == DIGEST and environment.image_id == IMAGE_ID
        assert environment.container.options["image"] == IMAGE_ID


def test_local_repository_digest_is_not_compared_to_an_image_configuration_id():
    client = Client()
    with create_preload_environment(inputs(image=DIGEST), client=client) as environment:
        assert environment.image_id == IMAGE_ID
        assert client.image_gets == [DIGEST] and not client.pulls


def test_missing_local_id_can_use_a_tag_but_must_get_the_expected_id():
    client = Client()
    client.image_missing = True
    with create_preload_environment(
        inputs(image_reference="registry.example/task:v1"), client=client,
    ):
        assert client.pulls == [("registry.example/task:v1", {"platform": "linux/amd64"})]
    client.image.id = OTHER_ID
    with pytest.raises(EnvironmentMismatch, match="image ID"), create_preload_environment(
        inputs(image_reference="registry.example/task:v1"), client=client,
    ):
        pytest.fail("must not create an environment for a changed tag")
    assert len(client.created) == 1


@pytest.mark.parametrize("reference", [None, IMAGE_ID])
def test_bare_local_id_cannot_be_pulled_without_a_repository_hint(reference):
    client = Client()
    client.image_missing = True
    with pytest.raises(SandboxError, match="requires a repository"), create_preload_environment(
        inputs(image_reference=reference), client=client,
    ):
        pytest.fail("must not yield")
    assert not client.pulls and not client.created


def test_pulling_can_be_disabled():
    client = Client()
    client.image_missing = True
    with pytest.raises(SandboxError, match="pulling is disabled"), create_preload_environment(
        inputs(image=DIGEST), client=client, allow_pull=False,
    ):
        pytest.fail("must not yield")
    assert not client.pulls and not client.created


def test_daemon_inspection_errors_do_not_trigger_a_pull():
    client = Client()
    client.inspect_error = APIError(503)
    with pytest.raises(SandboxError, match="cannot inspect"), create_preload_environment(
        inputs(image=DIGEST), client=client,
    ):
        pytest.fail("must not yield")
    assert not client.pulls and not client.created


def test_pull_failure_does_not_create_a_container():
    client = Client()
    client.image_missing, client.pull_error = True, APIError(401)
    with pytest.raises(SandboxError, match="pull failed"), create_preload_environment(
        inputs(image=DIGEST), client=client,
    ):
        pytest.fail("must not yield")
    assert not client.created


@pytest.mark.parametrize("damage", ["id", "platform", "volumes", "daemon"])
def test_incompatible_images_and_daemons_are_rejected_before_container_creation(damage):
    client = Client()
    if damage == "id":
        client.image.id = OTHER_ID
    elif damage == "platform":
        client.image.attrs["Architecture"] = "arm64"
    elif damage == "volumes":
        client.image.attrs["Config"]["Volumes"] = {"/data": {}}
    else:
        client.os_type = "windows"
    with pytest.raises(EnvironmentMismatch), create_preload_environment(inputs(), client=client):
        pytest.fail("must not yield")
    assert not client.created and not client.pulls


def test_digest_present_for_another_platform_can_be_pulled_once():
    client = Client()
    client.image.attrs["Architecture"] = "arm64"
    pull = client.images.pull

    def correct_platform(*args, **kwargs):
        image = pull(*args, **kwargs)
        image.attrs["Architecture"] = "amd64"
        return image

    client.images.pull = correct_platform
    with create_preload_environment(inputs(image=DIGEST), client=client):
        assert client.pulls == [(DIGEST, {"platform": "linux/amd64"})]


@pytest.mark.parametrize("damage", ["image", "workdir", "env", "mount", "started"])
def test_created_container_is_checked_and_cleaned_on_a_mismatch(damage):
    client = Client()

    def damage_container(container):
        if damage == "image":
            container.attrs["Image"] = OTHER_ID
        elif damage == "workdir":
            container.attrs["Config"]["WorkingDir"] = "/wrong"
        elif damage == "env":
            container.attrs["Config"]["Env"] = ["LANG=wrong"]
        elif damage == "mount":
            container.attrs["Mounts"] = [{"Destination": "/data"}]
        else:
            container.attrs["State"]["Status"] = "running"

    client.on_create = damage_container
    with pytest.raises(EnvironmentMismatch), create_preload_environment(inputs(), client=client):
        pytest.fail("must not yield")
    assert client.created[0].removed


@pytest.mark.parametrize("error", [RuntimeError, KeyboardInterrupt])
def test_body_failure_and_cancellation_remove_owned_containers(error):
    client = Client()
    with pytest.raises(error), create_preload_environment(inputs(), client=client):
        raise error("stop")
    assert client.created[0].removed


def test_lost_creation_response_recovers_only_the_owned_container():
    client = Client()
    client.lost_create = True
    with pytest.raises(OSError, match="create response lost"), create_preload_environment(
        inputs(), client=client,
    ):
        pytest.fail("must not yield")
    assert client.created[0].removed
    client.foreign = True
    with pytest.raises(PreloadCleanupError) as caught:
        with create_preload_environment(inputs(), client=client):
            pytest.fail("must not yield")
    assert isinstance(caught.value.__cause__, OSError)
    assert not client.created[-1].removed
    assert caught.value.container_name == client.created[-1].options["name"]


@pytest.mark.parametrize("failure", ["remove_error", "remove_noop"])
def test_cleanup_failures_are_explicit(failure):
    client = Client()
    setattr(client, failure, True)
    with pytest.raises(PreloadCleanupError) as caught:
        with create_preload_environment(inputs(), client=client):
            raise RuntimeError("primary failure")
    assert isinstance(caught.value.__cause__, RuntimeError)
    assert caught.value.container_id == client.created[0].id


def test_connection_ownership_and_client_close(monkeypatch):
    client = Client()
    timeouts = []

    def connect(timeout):
        timeouts.append(timeout)
        return client

    monkeypatch.setattr(preload_environment, "connect_docker", connect)
    with create_preload_environment(inputs(), api_timeout_sec=120):
        assert not client.closed
    assert timeouts == [120] and client.closed
    client.closed = False
    client.image_missing = True
    with pytest.raises(SandboxError):
        with create_preload_environment(inputs(), allow_pull=False):
            pytest.fail("must not yield")
    assert client.closed


def test_cleanup_failure_is_not_hidden_by_a_client_close_failure(monkeypatch):
    client = Client()
    client.remove_error = client.close_error = True
    monkeypatch.setattr(preload_environment, "connect_docker", lambda timeout: client)
    with pytest.raises(PreloadCleanupError) as caught:
        with create_preload_environment(inputs()):
            pass
    assert client.closed
    assert "close also failed" in caught.value.__notes__[0]


def test_client_close_failure_is_reported_when_no_other_failure_exists(monkeypatch):
    client = Client()
    client.close_error = True
    monkeypatch.setattr(preload_environment, "connect_docker", lambda timeout: client)
    with pytest.raises(SandboxCleanupError, match="cannot close"):
        with create_preload_environment(inputs()):
            pass
    assert client.created[0].removed


def test_input_configuration_is_copied_before_docker_calls():
    client, spec = Client(), inputs()
    client.on_get_image = lambda: spec.env.update(LANG="changed")
    with create_preload_environment(spec, client=client) as environment:
        assert environment.container.options["environment"] == {"LANG": "C.UTF-8"}


@pytest.mark.parametrize("options", [
    {"allow_pull": 1}, {"network_mode": "host"}, {"api_timeout_sec": 0},
    {"api_timeout_sec": True}, {"api_timeout_sec": float("inf")},
])
def test_invalid_options_fail_before_connecting(monkeypatch, options):
    monkeypatch.setattr(
        preload_environment, "connect_docker", lambda *a: pytest.fail("must not connect")
    )
    with pytest.raises(ValueError), create_preload_environment(inputs(), **options):
        pytest.fail("must not yield")
