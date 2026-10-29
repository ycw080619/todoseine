"""热键配置兼容性：新旧两种布局都要能解析出来。"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from chat2todo.qt.hotkey import resolve_specs  # noqa: E402


def test_legacy_layout():
    cfg = {"dash": {"hotkey": "ctrl+shift+d"}, "snap": {"hotkey": "ctrl+shift+s"}}
    specs = resolve_specs(cfg)
    assert specs["show"] == "ctrl+shift+d"
    assert specs["snap"] == "ctrl+shift+s"


def test_new_layout_wins():
    cfg = {"hotkey": {"show": "win+f1", "snap": "ctrl+f2"},
           "dash": {"hotkey": "ctrl+shift+d"}}
    specs = resolve_specs(cfg)
    assert specs["show"] == "win+f1"
    assert specs["snap"] == "ctrl+f2"


def test_defaults_when_missing():
    specs = resolve_specs({})
    assert specs["show"] == "ctrl+alt+d"
    assert specs["snap"] == "ctrl+shift+s"
    assert specs["enabled"] is True


def test_disabled_flag():
    assert resolve_specs({"hotkey": {"enabled": False}})["enabled"] is False