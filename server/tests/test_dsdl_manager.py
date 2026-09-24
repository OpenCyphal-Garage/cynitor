"""Tests for DsdlManager: the module-cache refresh, where custom types live, and compiling them."""

import sys
import types
from pathlib import Path

import pytest

from dsdl_manager import DsdlManager


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    (tmp_path / "dsdl_messages" / "public_regulated_data_types").mkdir(parents=True)
    (tmp_path / "dsdl_messages" / "custom").mkdir(parents=True)
    (tmp_path / "python_compiled_messages").mkdir()
    return tmp_path


@pytest.fixture
def mgr(project_root: Path) -> DsdlManager:
    return DsdlManager(project_root)


class TestRefreshPythonModuleCache:

    def test_drops_modules_under_compiled_dir(self, mgr: DsdlManager) -> None:
        (mgr.compiled_dir / "myapp").mkdir()
        sys.modules["myapp"] = types.ModuleType("myapp")
        sys.modules["myapp.sensors"] = types.ModuleType("myapp.sensors")
        sys.modules["unrelated_pkg"] = types.ModuleType("unrelated_pkg")
        try:
            mgr._refresh_python_module_cache()
            assert "myapp" not in sys.modules
            assert "myapp.sensors" not in sys.modules
            assert "unrelated_pkg" in sys.modules
        finally:
            sys.modules.pop("myapp", None)
            sys.modules.pop("myapp.sensors", None)
            sys.modules.pop("unrelated_pkg", None)

    def test_ignores_hidden_and_dunder_dirs(self, mgr: DsdlManager) -> None:
        (mgr.compiled_dir / ".cache").mkdir()
        (mgr.compiled_dir / "__pycache__").mkdir()
        sys.modules[".cache"] = types.ModuleType(".cache")
        sys.modules["__pycache__"] = types.ModuleType("__pycache__")
        try:
            mgr._refresh_python_module_cache()
            assert "__pycache__" in sys.modules
        finally:
            sys.modules.pop(".cache", None)
            sys.modules.pop("__pycache__", None)

    def test_no_compiled_dir_is_a_no_op(self, project_root: Path) -> None:
        no_compile_root = project_root / "elsewhere"
        no_compile_root.mkdir()
        (no_compile_root / "dsdl_messages" / "public_regulated_data_types").mkdir(parents=True)
        (no_compile_root / "dsdl_messages" / "custom").mkdir()
        mgr = DsdlManager(no_compile_root)
        assert not mgr.compiled_dir.is_dir()
        sys.modules["unrelated_pkg2"] = types.ModuleType("unrelated_pkg2")
        try:
            mgr._refresh_python_module_cache()
            assert "unrelated_pkg2" in sys.modules
        finally:
            sys.modules.pop("unrelated_pkg2", None)

    def test_empty_compiled_dir_is_a_no_op(self, mgr: DsdlManager) -> None:
        assert list(mgr.compiled_dir.iterdir()) == []
        sys.modules["unrelated_pkg3"] = types.ModuleType("unrelated_pkg3")
        try:
            mgr._refresh_python_module_cache()
            assert "unrelated_pkg3" in sys.modules
        finally:
            sys.modules.pop("unrelated_pkg3", None)


class TestRunCompilationRefreshHook:

    def test_successful_compile_invokes_refresh(self, mgr: DsdlManager, monkeypatch) -> None:
        (mgr.compiled_dir / "myapp").mkdir()
        sys.modules["myapp"] = types.ModuleType("myapp")
        sys.modules["myapp.sensors"] = types.ModuleType("myapp.sensors")
        monkeypatch.setattr(mgr, "_compile", lambda *a, **kw: [])
        try:
            result = mgr._run_compilation(scope="public")
            assert result == {"ok": True}
            assert "myapp" not in sys.modules
            assert "myapp.sensors" not in sys.modules
        finally:
            sys.modules.pop("myapp", None)
            sys.modules.pop("myapp.sensors", None)

    def test_failed_compile_does_not_invoke_refresh(self, mgr: DsdlManager, monkeypatch) -> None:
        (mgr.compiled_dir / "myapp").mkdir()
        sys.modules["myapp"] = types.ModuleType("myapp")
        monkeypatch.setattr(mgr, "_compile", lambda *a, **kw: ["custom/myapp: exit code 1"])
        try:
            result = mgr._run_compilation(scope="public")
            assert result["ok"] is False
            assert "errors" in result
            assert "myapp" in sys.modules
        finally:
            sys.modules.pop("myapp", None)

    def test_compile_errors_are_reported(self, mgr: DsdlManager, monkeypatch) -> None:
        def broken(*_args, **_kwargs):
            raise RuntimeError("syntax error in Foo.1.0.dsdl")
        import pycyphal.dsdl
        monkeypatch.setattr(pycyphal.dsdl, "compile", broken, raising=False)
        (mgr.custom_dir / "myapp").mkdir()
        (mgr.custom_dir / "myapp" / "Foo.1.0.dsdl").write_text("@sealed\n")
        result = mgr.compile_custom()
        assert result["ok"] is False
        assert "syntax error" in result["errors"][0]


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


class TestDataFolderLayout:
    """Custom types live in the data folder: the source tree is not writable
    inside the executable, and is emptied on every exit there."""

    def test_custom_types_and_their_code_go_to_the_data_folder(self, project_root, data_dir):
        mgr = DsdlManager(project_root, data_dir=data_dir)
        assert mgr.custom_dir == data_dir / "dsdl" / "custom"
        assert mgr.custom_compiled_dir == data_dir / "dsdl" / "compiled"
        # The public types stay where they are (built into the executable).
        assert mgr.compiled_dir == project_root / "python_compiled_messages"

    def test_saved_types_land_in_the_data_folder(self, project_root, data_dir):
        mgr = DsdlManager(project_root, data_dir=data_dir)
        result = mgr.save_type("myapp", "Reading", "1.0", "uint8 x\n@sealed\n")
        assert Path(result["path"]).parent == data_dir / "dsdl" / "custom" / "myapp"

    def test_earlier_custom_types_are_copied_not_moved(self, project_root, data_dir):
        # In a source checkout they may be under version control.
        old = project_root / "dsdl_messages" / "custom" / "myapp"
        old.mkdir(parents=True)
        (old / "Reading.1.0.dsdl").write_text("uint8 x\n@sealed\n")
        DsdlManager(project_root, data_dir=data_dir)
        assert (data_dir / "dsdl" / "custom" / "myapp" / "Reading.1.0.dsdl").is_file()
        assert (old / "Reading.1.0.dsdl").is_file()

    def test_existing_custom_types_are_never_overwritten(self, project_root, data_dir):
        old = project_root / "dsdl_messages" / "custom" / "myapp"
        old.mkdir(parents=True)
        (old / "Reading.1.0.dsdl").write_text("old")
        current = data_dir / "dsdl" / "custom" / "myapp"
        current.mkdir(parents=True)
        (current / "Reading.1.0.dsdl").write_text("current")
        DsdlManager(project_root, data_dir=data_dir)
        assert (current / "Reading.1.0.dsdl").read_text() == "current"

    def test_make_importable_adds_the_compiled_folder_once(self, project_root, data_dir, monkeypatch):
        monkeypatch.setattr(sys, "path", list(sys.path))
        mgr = DsdlManager(project_root, data_dir=data_dir)
        mgr.make_importable()
        mgr.make_importable()
        expected = str((data_dir / "dsdl" / "compiled").resolve())
        assert sys.path.count(expected) == 1

    def test_custom_types_compile_into_the_data_folder(self, project_root, data_dir, monkeypatch):
        mgr = DsdlManager(project_root, data_dir=data_dir)
        (mgr.custom_dir / "myapp").mkdir(parents=True)
        (mgr.custom_dir / "myapp" / "Reading.1.0.dsdl").write_text("uint8 x\n@sealed\n")
        calls = []
        monkeypatch.setattr(mgr, "_compile", lambda target, lookups, output, label: calls.append((target, output)) or [])
        assert mgr.compile_custom() == {"ok": True}
        assert calls == [(mgr.custom_dir / "myapp", data_dir / "dsdl" / "compiled")]

    def test_compiled_custom_type_is_recognised(self, project_root, data_dir):
        mgr = DsdlManager(project_root, data_dir=data_dir)
        compiled = data_dir / "dsdl" / "compiled" / "myapp"
        compiled.mkdir(parents=True)
        (compiled / "Reading_1_0.py").write_text("")
        assert mgr.is_compiled("myapp.Reading.1.0")

    def test_deleting_a_type_removes_its_compiled_code(self, project_root, data_dir):
        mgr = DsdlManager(project_root, data_dir=data_dir)
        mgr.save_type("myapp", "Reading", "1.0", "uint8 x\n@sealed\n")
        compiled = data_dir / "dsdl" / "compiled" / "myapp"
        compiled.mkdir(parents=True)
        (compiled / "Reading_1_0.py").write_text("")
        mgr.delete_type("myapp", "Reading", "1.0")
        assert not (compiled / "Reading_1_0.py").exists()


class TestExecutable:
    def test_public_types_are_not_recompiled(self, project_root, data_dir, monkeypatch):
        # They are built in; recompiling would write into the temporary folder.
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        mgr = DsdlManager(project_root, data_dir=data_dir)
        assert mgr.compile_public()["ok"] is False
        assert mgr.get_status()["public_compilable"] is False

    def test_compile_all_compiles_only_custom_types(self, project_root, data_dir, monkeypatch):
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        mgr = DsdlManager(project_root, data_dir=data_dir)
        (mgr.custom_dir / "myapp").mkdir(parents=True)
        labels = []
        monkeypatch.setattr(mgr, "_compile", lambda target, lookups, output, label: labels.append(label) or [])
        assert mgr.compile_all() == {"ok": True}
        assert labels == ["custom/myapp"]
