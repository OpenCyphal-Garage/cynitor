"""Tests for startup_setup project-root resolution (source tree vs frozen binary)."""

import sys
from pathlib import Path

from startup_setup import resolve_project_root


class TestResolveProjectRoot:
    def test_resolves_to_repository_root_from_source(self):
        root = resolve_project_root()
        # The root is the parent of server/, so it holds the DSDL definitions.
        assert (root / "dsdl_messages").is_dir()
        assert (root / "server" / "startup_setup.py").is_file()

    def test_uses_meipass_when_frozen(self, monkeypatch, tmp_path):
        # PyInstaller unpacks bundled data to sys._MEIPASS; the source tree is
        # absent in a frozen binary, so the root must follow the extraction dir.
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
        assert resolve_project_root() == Path(tmp_path)

    def test_ignores_meipass_when_not_frozen(self, monkeypatch, tmp_path):
        monkeypatch.delattr(sys, "frozen", raising=False)
        monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
        assert resolve_project_root() != Path(tmp_path)
