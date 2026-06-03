"""Tests for DsdlManager — focused on the module-cache refresh behavior."""

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
        monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/nnvg")
        monkeypatch.setattr(mgr, "_nnvg_compile", lambda *a, **kw: [])
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
        monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/nnvg")
        monkeypatch.setattr(mgr, "_nnvg_compile", lambda *a, **kw: ["custom/myapp: exit code 1"])
        try:
            result = mgr._run_compilation(scope="public")
            assert result["ok"] is False
            assert "errors" in result
            assert "myapp" in sys.modules
        finally:
            sys.modules.pop("myapp", None)

    def test_no_nnvg_returns_clean_error(self, mgr: DsdlManager, monkeypatch) -> None:
        monkeypatch.setattr("shutil.which", lambda name: None)
        result = mgr._run_compilation(scope="all")
        assert result["ok"] is False
        assert "nnvg not available" in result.get("error", "")
