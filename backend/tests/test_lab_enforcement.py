"""Regression coverage for required-lab enforcement in ``tests/conftest.py``.

``pytest.mark.fail`` is not a real marker, so require-mode used to skip silently
instead of failing. These tests drive the actual hook functions and pin both
branches: required mode must fail a lab-marked test, and the default mode must
keep skipping it.
"""

from __future__ import annotations

import pytest

from tests import conftest, lab_support


class _Config:
    """Minimal stand-in exposing only the state the hooks read."""

    def __init__(self) -> None:
        self.stash = pytest.Stash()


class _Item:
    """Minimal stand-in for a collected item."""

    def __init__(self, keywords: set[str], config: _Config) -> None:
        self.keywords = keywords
        self.config = config
        self.added: list[pytest.MarkDecorator] = []

    def add_marker(self, marker: pytest.MarkDecorator) -> None:
        self.added.append(marker)


def _items() -> tuple[_Config, _Item, _Item]:
    config = _Config()
    return config, _Item({"lab"}, config), _Item(set(), config)


def test_require_mode_fails_lab_marked_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    config, lab_item, plain_item = _items()
    monkeypatch.setattr(lab_support, "lab_available", lambda: False)
    monkeypatch.setenv("SSHSUITE_REQUIRE_LAB", "1")

    conftest.pytest_collection_modifyitems(config, [lab_item, plain_item])

    # No skip marker may be injected in require mode; the failure comes from setup.
    assert lab_item.added == []
    with pytest.raises(pytest.fail.Exception, match="SSH test lab not reachable"):
        conftest.pytest_runtest_setup(lab_item)
    conftest.pytest_runtest_setup(plain_item)  # unmarked tests stay unaffected


def test_default_mode_still_skips_lab_marked_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    config, lab_item, plain_item = _items()
    monkeypatch.setattr(lab_support, "lab_available", lambda: False)
    monkeypatch.delenv("SSHSUITE_REQUIRE_LAB", raising=False)

    conftest.pytest_collection_modifyitems(config, [lab_item, plain_item])

    assert len(lab_item.added) == 1
    assert lab_item.added[0].mark.name == "skip"
    assert "SSH test lab not reachable" in lab_item.added[0].mark.kwargs["reason"]
    assert plain_item.added == []
    conftest.pytest_runtest_setup(lab_item)  # skip mode must not raise


def test_available_lab_leaves_items_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    config, lab_item, plain_item = _items()
    monkeypatch.setattr(lab_support, "lab_available", lambda: True)
    monkeypatch.setenv("SSHSUITE_REQUIRE_LAB", "1")

    conftest.pytest_collection_modifyitems(config, [lab_item, plain_item])

    assert lab_item.added == [] and plain_item.added == []
    conftest.pytest_runtest_setup(lab_item)
