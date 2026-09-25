"""Tests for the data folder: where it is, and moving data left by earlier versions."""

import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

import data_dir
from data_dir import default_data_dir, prepare_data_dir, resolve_data_dir


class TestDefaultDataDir:
    def test_windows_uses_local_app_data(self):
        path = default_data_dir("win32", {"LOCALAPPDATA": r"C:\Users\me\AppData\Local"}, Path("/home/me"))
        assert path == Path(r"C:\Users\me\AppData\Local") / "Cynitor"

    def test_windows_without_local_app_data(self):
        assert default_data_dir("win32", {}, Path("/h")) == Path("/h/AppData/Local/Cynitor")

    def test_macos(self):
        assert default_data_dir("darwin", {}, Path("/h")) == Path("/h/Library/Application Support/Cynitor")

    def test_linux_follows_xdg(self):
        assert default_data_dir("linux", {"XDG_DATA_HOME": "/x"}, Path("/h")) == Path("/x/cynitor")

    def test_linux_default(self):
        assert default_data_dir("linux", {}, Path("/h")) == Path("/h/.local/share/cynitor")

    def test_systemd_service_uses_its_state_directory(self):
        # StateDirectory=cynitor in the unit: /var/lib/cynitor, not root's home.
        env = {"STATE_DIRECTORY": "/var/lib/cynitor", "XDG_DATA_HOME": "/x"}
        assert default_data_dir("linux", env, Path("/root")) == Path("/var/lib/cynitor")


class TestResolveDataDir:
    def test_command_line_wins(self):
        assert resolve_data_dir("/cli", {"CYNITOR_DATA_DIR": "/env"}) == Path("/cli")

    def test_then_environment(self):
        assert resolve_data_dir(None, {"CYNITOR_DATA_DIR": "/env"}) == Path("/env")

    def test_then_the_default(self):
        with patch.object(data_dir, "default_data_dir", return_value=Path("/default")):
            assert resolve_data_dir(None, {}) == Path("/default")


@pytest.fixture
def folders(tmp_path):
    old, new = tmp_path / "started-here", tmp_path / "data"
    old.mkdir()
    return old, new


class TestPrepareDataDir:
    def test_creates_the_folder(self, folders):
        old, new = folders
        assert prepare_data_dir(new, legacy_dir=old) == []
        assert new.is_dir()

    def test_moves_databases_with_their_journals(self, folders):
        # Committed transactions can still be in the -wal file; leaving it
        # behind would lose them.
        old, new = folders
        for name in ("telemetry_events.db", "telemetry_events.db-wal", "telemetry_events.db-shm",
                     "allocator_2app.db"):
            (old / name).write_text(name)
        moved = prepare_data_dir(new, legacy_dir=old)
        assert moved == ["telemetry_events.db", "allocator_2app.db"]
        assert (new / "telemetry_events.db-wal").read_text() == "telemetry_events.db-wal"
        assert not any(old.iterdir())

    def test_never_overwrites_existing_data(self, folders):
        old, new = folders
        new.mkdir()
        (new / "telemetry_events.db").write_text("current")
        (old / "telemetry_events.db").write_text("stale")
        assert prepare_data_dir(new, legacy_dir=old) == []
        assert (new / "telemetry_events.db").read_text() == "current"
        assert (old / "telemetry_events.db").read_text() == "stale"

    def test_leaves_unrelated_files_alone(self, folders):
        # The scanner's registers only hold settings it rebuilds.
        old, new = folders
        (old / "monitor_app.db").write_text("x")
        (old / "notes.txt").write_text("x")
        prepare_data_dir(new, legacy_dir=old)
        assert sorted(p.name for p in old.iterdir()) == ["monitor_app.db", "notes.txt"]

    def test_started_inside_the_data_folder(self, folders):
        old, _ = folders
        (old / "telemetry_events.db").write_text("x")
        assert prepare_data_dir(old, legacy_dir=old) == []
        assert (old / "telemetry_events.db").exists()

    def test_a_failed_move_puts_everything_back(self, folders):
        # Windows refuses to move a file another process has open. A database
        # must not end up separated from its -wal.
        old, new = folders
        (old / "telemetry_events.db").write_text("db")
        (old / "telemetry_events.db-wal").write_text("wal")
        real_move = shutil.move

        def move(src, dst):
            if src.endswith("-wal"):
                raise PermissionError("in use")
            return real_move(src, dst)

        with patch.object(data_dir.shutil, "move", side_effect=move):
            assert prepare_data_dir(new, legacy_dir=old) == []
        assert (old / "telemetry_events.db").read_text() == "db"
        assert (old / "telemetry_events.db-wal").read_text() == "wal"
        assert not (new / "telemetry_events.db").exists()


class TestSessionPaths:
    def test_session_keeps_its_data_folder(self, tmp_path):
        from main import CANSession
        assert CANSession(data_dir=tmp_path).data_dir == tmp_path
