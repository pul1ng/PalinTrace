import json

import pytest
from pydantic import ValidationError

from palinreplay.replay.service_config import load_services


def service(name="A", **overrides):
    return {"name": name, "start_command": "start " + name,
            "readiness_command": "probe " + name, "timeout_sec": 10, **overrides}


def load(tmp_path, values):
    path = tmp_path / "services.json"
    path.write_text(json.dumps({"services": values}))
    return load_services(path)


def test_single_service_preserves_commands_and_inherits_execution_settings(tmp_path):
    config = load(tmp_path, [service(start_command="  launch --flag  ")])
    item, = config.services
    assert item.name == "A" and item.start_command == "  launch --flag  "
    assert item.cwd is None and item.env == {} and item.timeout_sec == 10
    assert sorted(path.name for path in tmp_path.iterdir()) == ["services.json"]


def test_two_services_preserve_order_and_explicit_overrides(tmp_path):
    config = load(tmp_path, [service("B", cwd="/srv/B", env={"FLAG": "value"}), service("A")])
    assert [item.name for item in config.services] == ["B", "A"]
    assert config.services[0].cwd == "/srv/B" and config.services[0].env == {"FLAG": "value"}
    assert config.services[1].env == {}


@pytest.mark.parametrize("field", ["name", "start_command", "readiness_command", "timeout_sec"])
def test_missing_required_field_fails_without_creating_runtime_resources(tmp_path, field):
    item = service()
    del item[field]
    with pytest.raises(ValidationError, match=field):
        load(tmp_path, [item])
    assert sorted(path.name for path in tmp_path.iterdir()) == ["services.json"]


@pytest.mark.parametrize("overrides", [
    {"name": " "}, {"start_command": ""}, {"readiness_command": "\x00"},
    {"timeout_sec": 0}, {"timeout_sec": -1}, {"timeout_sec": float("inf")},
    {"timeout_sec": True}, {"timeout_sec": "10"}, {"cwd": "relative"},
    {"env": {"FLAG": 1}}, {"env": {"A=B": "value"}}, {"extra": "unknown"},
])
def test_invalid_definition_is_rejected(tmp_path, overrides):
    with pytest.raises(ValidationError):
        load(tmp_path, [service(**overrides)])


def test_duplicate_names_are_rejected(tmp_path):
    with pytest.raises(ValidationError, match="unique"):
        load(tmp_path, [service(), service()])


def test_malformed_file_is_rejected(tmp_path):
    path = tmp_path / "invalid.json"
    path.write_text("{broken")
    with pytest.raises(ValidationError):
        load_services(path)
