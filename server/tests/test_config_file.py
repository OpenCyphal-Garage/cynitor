"""Tests for cynitor.toml: where it is looked for, what it may hold, and what overrides it."""

import os
import re
import stat
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

import main
from config_file import SETTINGS, TEMPLATE, ConfigError, apply, executable_dir, find, load, write_template

# Every "#name = value" line of the template, taken in.
EVERY_SETTING = re.sub(r"^#(?=[a-z-]+ = )", "", TEMPLATE, flags=re.M)


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class TestWhereItIsLookedFor:
    def test_the_file_named_first(self, tmp_path):
        named = write(tmp_path / "mine.toml", "")
        assert find(str(named), tmp_path / "data", {"CYNITOR_CONFIG": "/elsewhere.toml"}) == (named, [named])

    def test_then_the_one_the_environment_names(self, tmp_path):
        named = write(tmp_path / "env.toml", "")
        assert find(None, tmp_path / "data", {"CYNITOR_CONFIG": str(named)})[0] == named

    def test_a_file_named_that_is_not_there_is_refused(self, tmp_path):
        with pytest.raises(ConfigError, match="no such settings file"):
            find(str(tmp_path / "missing.toml"), tmp_path, {})

    def test_then_next_to_the_appimage_before_the_data_folder(self, tmp_path):
        beside = write(tmp_path / "apps" / "cynitor.toml", "")
        write(tmp_path / "data" / "cynitor.toml", "")
        environ = {"APPIMAGE": str(tmp_path / "apps" / "Cynitor.AppImage")}
        assert find(None, tmp_path / "data", environ)[0] == beside.resolve()

    def test_then_the_data_folder(self, tmp_path):
        in_data = write(tmp_path / "cynitor.toml", "")
        assert find(None, tmp_path, {})[0] == in_data  # run from source: no executable's folder

    def test_none_found_says_where_it_looked(self, tmp_path):
        environ = {"APPIMAGE": str(tmp_path / "apps" / "Cynitor.AppImage")}
        assert find(None, tmp_path / "data", environ) == (
            None, [(tmp_path / "apps").resolve() / "cynitor.toml", tmp_path / "data" / "cynitor.toml"])


class TestExecutableDir:
    def test_the_appimage_file_not_its_mount(self, tmp_path):
        # The executable runs from the AppImage's mount; the user's file is beside the .AppImage.
        assert executable_dir({"APPIMAGE": str(tmp_path / "Cynitor.AppImage")}) == tmp_path.resolve()

    def test_a_frozen_executable(self, tmp_path):
        with patch.object(sys, "frozen", True, create=True), \
                patch.object(sys, "executable", str(tmp_path / "cynitor-server.exe")):
            assert executable_dir({}) == tmp_path.resolve()

    def test_none_from_source(self):
        assert executable_dir({}) is None


class TestWhatItMayHold:
    def test_settings_named_as_the_options_are(self, tmp_path):
        path = write(tmp_path / "c.toml", 'bind = "0.0.0.0"\nport = 9000\nlisten-only = true\nbitrate = 1000000\n')
        assert load(path) == {"bind": "0.0.0.0", "port": 9000, "listen-only": True, "bitrate": 1000000}

    def test_an_unknown_setting_is_refused_naming_the_one_meant(self, tmp_path):
        with pytest.raises(ConfigError, match="there is no setting 'listen_only'; did you mean 'listen-only'"):
            load(write(tmp_path / "c.toml", "listen_only = true\n"))

    def test_a_section_is_no_setting(self, tmp_path):
        with pytest.raises(ConfigError, match="there is no setting 'server'"):
            load(write(tmp_path / "c.toml", '[server]\nbind = "0.0.0.0"\n'))

    @pytest.mark.parametrize("line, why", [
        ('port = "8080"', "port must be a whole number"),
        ("port = true", "port must be a whole number"),
        ('listen-only = "yes"', "listen-only must be true or false"),
        ("bind = 0", "bind must be a string"),
        ("port = 70000", "port must be between 1 and 65535"),
        ("node-id = 128", "node-id must be between 0 and 127"),
        ("bitrate = 0", "bitrate: CAN bitrate must be between"),
        ("data-bitrate = -1", "data-bitrate: CAN FD data bitrate must be between"),
    ])
    def test_a_value_it_cannot_take_is_refused(self, tmp_path, line, why):
        with pytest.raises(ConfigError, match=re.escape(why)):
            load(write(tmp_path / "c.toml", line + "\n"))

    def test_a_windows_path_in_single_quotes_is_taken_as_written(self, tmp_path):
        assert load(write(tmp_path / "c.toml", "data-dir = 'D:\\Cynitor data'\n")) == {"data-dir": "D:\\Cynitor data"}

    def test_a_windows_path_in_double_quotes_is_refused_naming_the_file(self, tmp_path):
        path = write(tmp_path / "c.toml", 'data-dir = "D:\\Cynitor data"\n')  # \C: no escape TOML has
        with pytest.raises(ConfigError, match=re.escape(str(path))):
            load(path)

    def test_a_file_that_does_not_parse_is_named(self, tmp_path):
        path = write(tmp_path / "c.toml", "bind = \n")
        with pytest.raises(ConfigError, match=re.escape(str(path))):
            load(path)


class TestTemplate:
    def test_as_written_it_sets_nothing(self, tmp_path):
        assert load(write(tmp_path / "c.toml", TEMPLATE)) == {}

    def test_it_lists_every_setting_with_a_value_it_takes(self, tmp_path):
        assert set(load(write(tmp_path / "c.toml", EVERY_SETTING))) == set(SETTINGS)

    def test_the_defaults_it_shows_are_the_options_defaults(self, tmp_path):
        parser = main.build_parser()
        for name, value in load(write(tmp_path / "c.toml", EVERY_SETTING)).items():
            target = SETTINGS[name][1]
            if not target.startswith("env:") and parser.get_default(target) is not None:
                assert value == parser.get_default(target), name

    def test_written_for_its_owner_only_and_never_over_a_file(self, tmp_path):
        path = tmp_path / "folder" / "cynitor.toml"
        write_template(path)
        assert path.read_text(encoding="utf-8") == TEMPLATE
        if os.name == "posix":
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
        with pytest.raises(FileExistsError):
            write_template(path)


class TestWhatOverridesIt:
    @staticmethod
    def parse(settings, argv, environ, path=Path("/etc/cynitor/cynitor.toml")):
        parser = main.build_parser()
        apply(settings, path, parser, environ)
        return parser.parse_args(argv)

    def test_the_file_sets_the_defaults(self):
        args = self.parse({"bind": "0.0.0.0", "port": 9000, "listen-only": True}, [], {})
        assert (args.bind, args.port, args.listen_only) == ("0.0.0.0", 9000, True)

    def test_the_command_line_overrides_it(self):
        args = self.parse({"bind": "0.0.0.0", "port": 9000}, ["--port", "9001"], {})
        assert (args.bind, args.port) == ("0.0.0.0", 9001)

    def test_the_environment_overrides_a_setting_that_stands_for_a_variable(self):
        environ = {"CYNITOR_AUTH_TOKEN": "from-the-environment"}
        self.parse({"auth-token": "from-the-file", "node-id": 42}, [], environ)
        assert environ == {"CYNITOR_AUTH_TOKEN": "from-the-environment", "UAVCAN__NODE__ID": "42"}

    def test_a_relative_data_folder_is_taken_from_the_files_folder(self, tmp_path):
        environ = {}
        self.parse({"data-dir": "data"}, [], environ, path=tmp_path / "cynitor.toml")
        assert environ == {"CYNITOR_DATA_DIR": str(tmp_path / "data")}

    def test_an_absolute_data_folder_stays_as_it_is(self, tmp_path):
        environ = {}
        self.parse({"data-dir": str(tmp_path / "x")}, [], environ)
        assert environ == {"CYNITOR_DATA_DIR": str(tmp_path / "x")}


class TestStartup:
    @pytest.fixture(autouse=True)
    def plain_environment(self, monkeypatch):
        for name in ("CYNITOR_CONFIG", "APPIMAGE", "STATE_DIRECTORY"):
            monkeypatch.delenv(name, raising=False)

    def test_a_first_start_writes_a_template_to_edit(self, tmp_path):
        said = main._read_config(main.build_parser(), None, tmp_path)
        assert (tmp_path / "cynitor.toml").read_text(encoding="utf-8") == TEMPLATE
        assert said == f"none yet; {tmp_path / 'cynitor.toml'} lists them all, to edit"

    def test_the_service_gets_none_as_its_settings_are_in_etc_default(self, tmp_path, monkeypatch):
        monkeypatch.setenv("STATE_DIRECTORY", str(tmp_path))
        said = main._read_config(main.build_parser(), None, tmp_path)
        assert not (tmp_path / "cynitor.toml").exists() and said.endswith("; the defaults")

    def test_a_file_found_is_applied_and_named_with_what_it_sets(self, tmp_path):
        write(tmp_path / "cynitor.toml", "port = 9000\n")
        parser = main.build_parser()
        said = main._read_config(parser, None, tmp_path)
        assert (parser.parse_args([]).port, said) == (9000, f"{tmp_path / 'cynitor.toml'} (port)")

    def test_the_template_found_sets_nothing_and_says_so(self, tmp_path):
        write(tmp_path / "cynitor.toml", TEMPLATE)
        said = main._read_config(main.build_parser(), None, tmp_path)
        assert said == f"{tmp_path / 'cynitor.toml'} (all at their defaults)"

    def test_a_file_that_cannot_be_used_stops_the_start(self, tmp_path, capsys):
        write(tmp_path / "cynitor.toml", "listen_only = true\n")
        with pytest.raises(SystemExit) as stopped:
            main._read_config(main.build_parser(), None, tmp_path)
        assert stopped.value.code == 2 and "did you mean 'listen-only'" in capsys.readouterr().err

    def test_write_config_to_the_data_folder_or_one_named_never_over_a_file(self, tmp_path):
        assert main._write_config("", tmp_path / "data") == 0
        assert (tmp_path / "data" / "cynitor.toml").read_text(encoding="utf-8") == TEMPLATE
        assert main._write_config(str(tmp_path), tmp_path / "data") == 0  # a folder: the file in it
        assert (tmp_path / "cynitor.toml").is_file()
        assert main._write_config(str(tmp_path / "cynitor.toml"), tmp_path / "data") == 1
