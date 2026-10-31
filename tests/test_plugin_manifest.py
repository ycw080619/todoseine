"""插件 manifest 校验：格式正确的放行，错的拒。"""
from __future__ import annotations

import json
import os
import tempfile
import unittest

from todoseine.plugin_manifest import ManifestError, validate_manifest, SCHEMA


class TestPluginManifest(unittest.TestCase):
    GOOD = {
        "schema": SCHEMA,
        "id": "snap_todo",
        "name": "截图待办",
        "version": "0.1.0",
        "description": "Win+Shift+S",
        "permission_level": "uia",
        "entry": "snap.py",
        "capabilities": ["ocr", "hotkey"],
        "dependencies": ["todoseine>=0.4.0"],
        "triggers": {"autostart": True, "hotkey": "ctrl+shift+s"},
    }

    def test_good_pass(self):
        m = validate_manifest(self.GOOD)
        self.assertEqual(m.id, "snap_todo")
        self.assertEqual(m.permission_level, "uia")
        self.assertEqual(m.capabilities, ["ocr", "hotkey"])
        self.assertTrue(m.triggers["autostart"])

    def test_good_via_file(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as fh:
            json.dump(self.GOOD, fh)
            path = fh.name
        try:
            m = validate_manifest(path)
            self.assertEqual(m.id, "snap_todo")
        finally:
            os.unlink(path)

    def test_bad_schema_rejected(self):
        bad = dict(self.GOOD, schema="old-v0")
        with self.assertRaises(ManifestError) as ctx:
            validate_manifest(bad)
        self.assertIn("schema", ctx.exception.field)

    def test_bad_perm_rejected(self):
        bad = dict(self.GOOD, permission_level="admin")
        with self.assertRaises(ManifestError) as ctx:
            validate_manifest(bad)
        self.assertIn("permission_level", ctx.exception.field)

    def test_bad_id_case_rejected(self):
        bad = dict(self.GOOD, id="SnapTodo")
        with self.assertRaises(ManifestError) as ctx:
            validate_manifest(bad)
        self.assertIn("id", ctx.exception.field)

    def test_missing_id_rejected(self):
        bad = {k: v for k, v in self.GOOD.items() if k != "id"}
        with self.assertRaises(ManifestError) as ctx:
            validate_manifest(bad)
        self.assertIn("id", ctx.exception.field)

    def test_default_perm_is_file(self):
        d = {k: v for k, v in self.GOOD.items() if k != "permission_level"}
        m = validate_manifest(d)
        self.assertEqual(m.permission_level, "file")

    def test_optional_fields_defaults(self):
        minimal = {"schema": SCHEMA, "id": "x", "name": "x", "version": "0.1"}
        m = validate_manifest(minimal)
        self.assertEqual(m.description, "")
        self.assertEqual(m.capabilities, [])
        self.assertEqual(m.dependencies, [])
        self.assertEqual(m.triggers, {})
