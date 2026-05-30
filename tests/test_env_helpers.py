"""Tests for ``shared.env`` coercion helpers."""

from __future__ import annotations

import pytest

from stt_server.env import env_bool, env_first, env_float, env_int

_VAR = "KODA_TEST_ENV_HELPER"


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv(_VAR, raising=False)
    monkeypatch.delenv("A", raising=False)
    monkeypatch.delenv("B", raising=False)


@pytest.mark.parametrize(
    "val,expected",
    [
        ("1", True),
        ("true", True),
        ("True", True),
        ("TRUE", True),
        ("yes", True),
        ("on", True),
        ("  on  ", True),
        ("False", False),
        ("false", False),
        ("0", False),
        ("no", False),
        ("", False),  # explicitly set empty != unset; treated as False
        ("garbage", False),
    ],
)
def test_env_bool_set(monkeypatch, val, expected):
    monkeypatch.setenv(_VAR, val)
    # default=True so we can tell whether "" returned default vs False
    assert env_bool(_VAR, default=True) is expected


def test_env_bool_unset_returns_default():
    assert env_bool(_VAR, default=True) is True
    assert env_bool(_VAR, default=False) is False


def test_env_float_valid(monkeypatch):
    monkeypatch.setenv(_VAR, "2.5")
    assert env_float(_VAR, 0.0) == 2.5


def test_env_float_invalid_returns_default(monkeypatch):
    monkeypatch.setenv(_VAR, "not-a-float")
    assert env_float(_VAR, 1.5) == 1.5


def test_env_float_unset_or_empty_returns_default(monkeypatch):
    assert env_float(_VAR, 1.5) == 1.5
    monkeypatch.setenv(_VAR, "")
    assert env_float(_VAR, 1.5) == 1.5


def test_env_int_valid(monkeypatch):
    monkeypatch.setenv(_VAR, "42")
    assert env_int(_VAR, 0) == 42


def test_env_int_invalid_returns_default(monkeypatch):
    monkeypatch.setenv(_VAR, "3.14")
    assert env_int(_VAR, 7) == 7


def test_env_first_returns_first_set(monkeypatch):
    monkeypatch.setenv("A", "alpha")
    monkeypatch.setenv("B", "beta")
    assert env_first("A", "B") == "alpha"


def test_env_first_skips_empty(monkeypatch):
    monkeypatch.setenv("A", "")
    monkeypatch.setenv("B", "beta")
    assert env_first("A", "B") == "beta"


def test_env_first_returns_default_when_none_set():
    assert env_first("A", "B", default="fallback") == "fallback"
    assert env_first("A", "B") is None
