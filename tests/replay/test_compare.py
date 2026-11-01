from types import SimpleNamespace

import pytest

from palinreplay.replay.compare import compare_tool
from palinreplay.sandbox.base import ExecResult


@pytest.mark.parametrize("old,new,expected", [(0, 1, False), (1, 0, False), (3, 3, True)])
def test_exit_status_both_directions_and_equal_known_error(old, new, expected):
    original = SimpleNamespace(raw=b"output", details={"backend": {"decoded": {"returncode": old}}},
                               source={"execution_id": "source"})
    result = compare_tool(original, ExecResult("command", b"output", new, 123))
    assert result["equal"] is expected
    assert result["definitive"]
    assert result["timing"]["actual_duration_sec"] == 123


def test_raw_output_is_compared_without_decoding_or_newline_normalization():
    old = SimpleNamespace(raw=b"a\xff\r\n", details={"backend": {"decoded": {"returncode": 0}}},
                          source={"execution_id": "source"})
    assert not compare_tool(old, ExecResult("cmd", b"a\xff\n", 0, 1))["equal"]
    assert not compare_tool(old, ExecResult("cmd", b"a\xfe\r\n", 0, 1))["equal"]
    assert compare_tool(old, ExecResult("cmd", b"a\xff\r\n", 0, 2))["equal"]
    unknown = compare_tool(old, SimpleNamespace(exit_code=None, output=b"", duration_sec=1))
    assert unknown["status"] == "unknown" and not unknown["definitive"]
