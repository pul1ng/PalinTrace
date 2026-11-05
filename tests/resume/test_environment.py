from copy import deepcopy
from types import SimpleNamespace

import pytest

from palinreplay.core.errors import IntegrityError
from palinreplay.resume.environment import saved_execution_config


@pytest.mark.parametrize("change", [
    {"cwd": "relative"}, {"env": {"BAD=KEY": "x"}}, {"timeout": True}, {"timeout": 0},
    {"timeout": float("nan")}, {"interpreter": ["sh", "-c"]}, {"forward_env": ["HOST_VAR"]},
])
def test_saved_execution_settings_are_checked_before_restore(change):
    config = {"cwd": "/testbed", "env": {"PAGER": "cat", "TZ": "Etc/UTC"},
              "interpreter": ["bash", "-c"], "timeout": 60, "forward_env": []}
    materials = SimpleNamespace(config={"environment": config},
                                context={"config": {"environment": config}},
                                runtime=SimpleNamespace(launch=SimpleNamespace(shell=("bash", "-c"))))
    assert saved_execution_config(materials) == config
    config.update(change)
    with pytest.raises(IntegrityError, match="execution configuration"):
        saved_execution_config(materials)
    materials.context["config"]["environment"] = deepcopy(config) | {"timeout": 32}
    with pytest.raises(IntegrityError, match="differ"):
        saved_execution_config(materials)
