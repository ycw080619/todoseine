"""旧配置文件兜底迁移：只补空值，不覆盖新值。"""

from __future__ import annotations

import textwrap

from chat2todo import config as c2t


def _write(path, text):
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return str(path)


def _defaults(tmp_path):
    """不触发兜底的默认配置（显式传一个不存在的路径）。"""
    return c2t.load(str(tmp_path / "absent.toml"))


def test_legacy_fills_empty(tmp_path, monkeypatch):
    legacy = tmp_path / "c2t.toml"
    _write(legacy, """
        db_path = "D:/legacy/qq.db"

        [ai]
        enabled = true
        base_url = "http://127.0.0.1:11434/v1"
        model = "qwen2.5:14b"

        [filter]
        ignore_groups = ["111", "222"]

        [dash]
        hotkey = "ctrl+shift+d"

        [snap]
        hotkey = "ctrl+shift+s"
    """)
    monkeypatch.setattr(c2t, "_LEGACY_CONFIG", str(legacy))

    cfg = _defaults(tmp_path)
    c2t._legacy_fill(cfg)
    assert cfg["db_path"] == "D:/legacy/qq.db"
    assert cfg["ai"]["enabled"] is True
    assert cfg["ai"]["model"] == "qwen2.5:14b"
    assert cfg["filter"]["ignore_groups"] == ["111", "222"]
    assert cfg["hotkey"]["show"] == "ctrl+shift+d"
    assert cfg["hotkey"]["snap"] == "ctrl+shift+s"


def test_new_values_win(tmp_path, monkeypatch):
    legacy = tmp_path / "c2t.toml"
    _write(legacy, """
        [ai]
        enabled = true
        model = "legacy-model"

        [filter]
        ignore_groups = ["111"]

        [dash]
        hotkey = "ctrl+shift+d"
    """)
    monkeypatch.setattr(c2t, "_LEGACY_CONFIG", str(legacy))

    cfg = _defaults(tmp_path)
    cfg["ai"]["model"] = "new-model"
    cfg["filter"]["ignore_groups"] = ["999"]
    cfg["hotkey"]["show"] = "win+f1"

    c2t._legacy_fill(cfg)
    assert cfg["ai"]["model"] == "new-model"
    assert cfg["filter"]["ignore_groups"] == ["999"]
    assert cfg["hotkey"]["show"] == "win+f1"


def test_missing_legacy_is_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(c2t, "_LEGACY_CONFIG", str(tmp_path / "absent.toml"))
    cfg = _defaults(tmp_path)
    c2t._legacy_fill(cfg)
    assert cfg["hotkey"]["show"] == "ctrl+alt+d"