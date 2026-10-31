import socket

import pytest


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("tests must not open network connections")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)


@pytest.fixture
def row():
    # Synthetic test data, not a benchmark instance or reference solution.
    return {
        "instance_id": "example__repo-1",
        "repo": "example/repo",
        "base_commit": "a" * 40,
        "version": "1.0",
        "problem_statement": "Fix the example bug.\nKeep the public API unchanged.",
        "hints_text": "private evaluation hint",
        "patch": "synthetic gold patch",
        "test_patch": "synthetic hidden test patch",
        "FAIL_TO_PASS": '["test_example"]',
        "PASS_TO_PASS": '["test_existing"]',
        "created_at": "2026-01-01",
        "unknown_upstream_field": {"preserve": True},
    }
