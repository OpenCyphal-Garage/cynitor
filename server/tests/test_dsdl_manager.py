"""Tests for DsdlManager: the module-cache refresh, where custom types live, and compiling them."""

import sys
import threading
import types
from pathlib import Path

import pytest

import dsdl_manager
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
            assert "exit code 1" in result["error"]
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
        assert "syntax error" in result["error"]


class TestCompileErrors:
    """A failed compile names each type it fails in as one reads it, not by
    where its file is on the server."""

    def test_the_type_and_its_line(self, project_root, data_dir, monkeypatch) -> None:
        # pycyphal is stubbed in these tests; the errors come from its front
        # end, pydsdl, reading each namespace, which this does for real.
        import pycyphal.dsdl
        import pydsdl
        monkeypatch.setattr(pycyphal.dsdl, "compile", lambda target, lookups, output_directory:
                            pydsdl.read_namespace(str(target), [str(d) for d in lookups]), raising=False)
        mgr = DsdlManager(project_root, data_dir=data_dir)
        mgr.save_type("myapp.sensors", "Reading", "1.0", "uint8 x\nnot_a_type y\n@sealed\n", 6200)
        mgr.save_type("other", "Sealless", "1.0", "uint8 x\n")
        error = mgr.compile_custom()["error"]
        syntax, sealless = error.split("\n")
        assert syntax == "myapp.sensors.Reading.1.0, line 2: Syntax error", error
        assert sealless.startswith("other.Sealless.1.0: ") and "@sealed" in sealless, error
        assert str(data_dir) not in error


class TestTreeEntries:

    def test_a_type_lists_its_field_and_constant_names_for_search(self, mgr: DsdlManager) -> None:
        mgr.save_type("myapp", "Reading", "1.0", "uint8 MAX = 3   # the most\nuint8 value\nvoid8\n@sealed\n")
        [entry] = mgr.get_namespaces()["namespaces"]["myapp"]["types"]
        assert (entry["field_names"], entry["constant_names"]) == (["value"], ["MAX"])


class TestStatus:
    """Each open DSDL tab asks for the status every 4 s; reading it walks
    every source and compiled file, so it is kept until something changes."""

    @staticmethod
    def _count_walks(monkeypatch) -> list:
        walks = []
        walk = DsdlManager._max_compiled_mtime
        monkeypatch.setattr(DsdlManager, "_max_compiled_mtime",
                            lambda self, *a, **kw: walks.append(1) or walk(self, *a, **kw))
        return walks

    def test_polls_read_the_folders_once(self, mgr: DsdlManager, monkeypatch) -> None:
        walks = self._count_walks(monkeypatch)
        first = mgr.get_status()
        once = len(walks)
        assert (mgr.get_status(), len(walks)) == (first, once)

    def test_a_change_made_here_shows_at_once(self, mgr: DsdlManager) -> None:
        assert mgr.get_status()["custom_types"] == 0
        mgr.save_type("myapp", "Reading", "1.0", "uint8 x\n@sealed\n")
        assert mgr.get_status()["custom_types"] == 1

    def test_one_made_elsewhere_within_half_a_minute(self, mgr: DsdlManager, monkeypatch) -> None:
        # Such as the public types, compiled when CAN connects if they are not yet.
        clock = types.SimpleNamespace(now=1000.0)
        monkeypatch.setattr(dsdl_manager, "time", types.SimpleNamespace(monotonic=lambda: clock.now))
        assert mgr.get_status()["compiled"] is False
        for root in ("uavcan", "reg"):
            (mgr.compiled_dir / root).mkdir()
        clock.now += 31
        assert mgr.get_status()["compiled"] is True

    def test_a_read_a_change_overlaps_is_not_kept(self, mgr: DsdlManager, monkeypatch) -> None:
        # A poll reading while a compile ends would otherwise keep what it read
        # before the compile, after the compile had reset it.
        reading, release = threading.Event(), threading.Event()
        walk = DsdlManager._max_compiled_mtime

        def slow_walk(self, *a, **kw):
            reading.set()
            release.wait(5)
            return walk(self, *a, **kw)

        monkeypatch.setattr(DsdlManager, "_max_compiled_mtime", slow_walk)
        poll = threading.Thread(target=mgr.get_status)
        poll.start()
        reading.wait(5)
        change = threading.Thread(target=mgr.invalidate_cache)
        change.start()
        change.join(0.2)  # done by now, unless it waits for the poll to end
        release.set()
        poll.join(5)
        change.join(5)
        walks = self._count_walks(monkeypatch)
        mgr.get_status()
        assert walks, "not read again after the change"


class TestDeleteNamespace:
    """A custom namespace with no types left in it can go, with its empty
    sub-namespaces; one that still has types cannot."""

    def test_an_empty_one_goes_with_its_empty_sub_namespaces(self, mgr: DsdlManager) -> None:
        mgr.create_namespace("myapp.sensors")
        assert mgr.delete_namespace("myapp") == {"namespace": "myapp", "deleted": True}
        assert mgr.list_custom_namespaces() == []

    def test_one_with_types_stays(self, mgr: DsdlManager) -> None:
        mgr.save_type("myapp.sensors", "Reading", "1.0", "uint8 x\n@sealed\n")
        with pytest.raises(ValueError):
            mgr.delete_namespace("myapp")
        assert mgr.list_custom_namespaces() == ["myapp", "myapp.sensors"]

    def test_an_empty_sub_namespace_alone(self, mgr: DsdlManager) -> None:
        mgr.save_type("myapp", "Reading", "1.0", "uint8 x\n@sealed\n")
        mgr.create_namespace("myapp.spare")
        mgr.delete_namespace("myapp.spare")
        assert mgr.list_custom_namespaces() == ["myapp"]

    def test_one_not_there(self, mgr: DsdlManager) -> None:
        with pytest.raises(FileNotFoundError):
            mgr.delete_namespace("nowhere")


_MESSAGE = "uint8 x\n@sealed\n"
_SERVICE = "uint8 x\n@sealed\n---\nuint8 y\n@sealed\n"


class TestFixedPortId:
    """Outside the uavcan namespace the compiler accepts fixed port-IDs only in
    the vendor ranges: 6144-7167 for a message, 256-383 for a service."""

    @pytest.mark.parametrize("port, source", [
        (-5, _MESSAGE), ("abc", _MESSAGE), (True, _MESSAGE), (100, _MESSAGE),
        (7509, _MESSAGE), (300, _MESSAGE), (7000, _SERVICE),
    ])
    def test_refused_and_nothing_written(self, mgr: DsdlManager, port, source) -> None:
        with pytest.raises(ValueError):
            mgr.save_type("myapp", "Foo", "1.0", source, fixed_port_id=port)
        assert not list(mgr.custom_dir.rglob("*.dsdl"))

    @pytest.mark.parametrize("port, source", [
        (None, _MESSAGE), (6144, _MESSAGE), (7167, _MESSAGE), (256, _SERVICE), (383, _SERVICE),
    ])
    def test_accepted(self, mgr: DsdlManager, port, source) -> None:
        mgr.save_type("myapp", "Foo", "1.0", source, fixed_port_id=port)
        [saved] = mgr.get_namespaces()["namespaces"]["myapp"]["types"]
        assert (saved["full_name"], saved["fixed_port_id"]) == ("myapp.Foo.1.0", port)


class TestPublicRootNamespaces:
    """A custom namespace under uavcan or reg would merge into the public
    types' own, in the tree and in the compiled code."""

    @pytest.mark.parametrize("namespace", ["uavcan", "reg", "uavcan.myext", "reg.udral.mine"])
    def test_not_created(self, mgr: DsdlManager, namespace: str) -> None:
        with pytest.raises(ValueError):
            mgr.create_namespace(namespace)
        assert not list(mgr.custom_dir.iterdir())

    def test_no_type_saved_into_them(self, mgr: DsdlManager) -> None:
        with pytest.raises(ValueError):
            mgr.save_type("uavcan.node", "Heartbeat", "1.0", "uint8 x\n@sealed\n")
        assert not list(mgr.custom_dir.iterdir())

    def test_names_that_only_start_alike_are_fine(self, mgr: DsdlManager) -> None:
        mgr.create_namespace("uavcanx")
        mgr.create_namespace("regulator")
        assert mgr.list_custom_namespaces() == ["regulator", "uavcanx"]

    def test_one_saved_before_can_still_be_deleted(self, mgr: DsdlManager) -> None:
        old = mgr.custom_dir / "uavcan" / "node"
        old.mkdir(parents=True)
        (old / "Heartbeat.1.0.dsdl").write_text("uint8 x\n@sealed\n")
        mgr.delete_type("uavcan.node", "Heartbeat", "1.0")
        assert not (old / "Heartbeat.1.0.dsdl").exists()


_PUBLIC_TYPES = {
    "uavcan/node/7509.Heartbeat.1.0.dsdl": """\
# Abstract node status information.
#
# Every node publishes it.

uint16 MAX_PUBLICATION_PERIOD = 1   # [second]

uint32 uptime                       # [second]
# Seconds since the node started.

uint8 vendor_specific_status_code
@extent 12 * 8
""",
    "uavcan/primitive/Empty.1.0.dsdl": "@sealed\n",
    "uavcan/register/Value.1.0.dsdl": """\
# One value of several kinds.
@union
uavcan.primitive.Empty.1.0 empty    # Tag 0: nothing
uint8 natural8                      # Tag 1: a small number
@sealed
""",
    "uavcan/node/430.GetInfo.1.0.dsdl": """\
# Full node info request.
@sealed
---
uint8[<=50] name                    # Human-readable name.
@extent 448 * 8
""",
    "uavcan/node/Old.1.0.dsdl": "@deprecated\nuint8 x\n@sealed\n",
}


class TestTypeDetailFromTheCompiler:
    """What the compiler (pydsdl) reads in a type beyond its fields: its
    comments, whether it is a union, sealed or how far it may grow, its
    size, and whether it is deprecated."""

    @pytest.fixture
    def mgr(self, project_root: Path) -> DsdlManager:
        for rel, text in _PUBLIC_TYPES.items():
            path = project_root / "dsdl_messages" / "public_regulated_data_types" / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        return DsdlManager(project_root)

    def test_a_message(self, mgr: DsdlManager) -> None:
        detail = mgr.get_type_detail("uavcan.node.Heartbeat.1.0")
        assert detail["doc"].startswith("Abstract node status information.")
        assert detail["deprecated"] is False
        assert detail["layout"] == {"union": False, "sealed": False, "extent_bytes": 12, "size_bytes": [5, 5]}
        assert [(f["name"], f["doc"]) for f in detail["fields"]] == [
            ("uptime", "[second]\nSeconds since the node started."), ("vendor_specific_status_code", "")]
        assert [(c["name"], c["doc"]) for c in detail["constants"]] == [("MAX_PUBLICATION_PERIOD", "[second]")]

    def test_a_union(self, mgr: DsdlManager) -> None:
        detail = mgr.get_type_detail("uavcan.register.Value.1.0")
        assert detail["layout"] == {"union": True, "sealed": True, "extent_bytes": 2, "size_bytes": [1, 2]}
        assert [f["doc"] for f in detail["fields"]] == ["Tag 0: nothing", "Tag 1: a small number"]

    def test_a_service_has_a_layout_each_way(self, mgr: DsdlManager) -> None:
        detail = mgr.get_type_detail("uavcan.node.GetInfo.1.0")
        assert detail["doc"] == "Full node info request."
        assert detail["layout"] == {
            "request": {"union": False, "sealed": True, "extent_bytes": 0, "size_bytes": [0, 0]},
            "response": {"union": False, "sealed": False, "extent_bytes": 448, "size_bytes": [1, 51]},
        }
        assert detail["fields"]["response"][0]["doc"] == "Human-readable name."

    def test_deprecated(self, mgr: DsdlManager) -> None:
        assert mgr.get_type_detail("uavcan.node.Old.1.0")["deprecated"] is True

    def test_without_pydsdl_the_detail_is_as_before(self, mgr: DsdlManager, monkeypatch) -> None:
        monkeypatch.setitem(sys.modules, "pydsdl", None)  # import fails
        detail = mgr.get_type_detail("uavcan.node.Heartbeat.1.0")
        assert (detail["doc"], detail["deprecated"], detail["layout"], detail["problem"]) == ("", False, None, None)
        assert [f["name"] for f in detail["fields"]] == ["uptime", "vendor_specific_status_code"]

    def test_a_type_that_compiles_has_no_problem(self, mgr: DsdlManager) -> None:
        mgr.save_type("myapp", "Good", "1.0", "uavcan.node.Heartbeat.1.0 beat\n@sealed\n")
        assert mgr.get_type_detail("myapp.Good.1.0")["problem"] is None

    def test_why_a_type_does_not_compile_and_on_which_line(self, mgr: DsdlManager) -> None:
        mgr.save_type("myapp", "Typo", "1.0", "uint8 a\nuint9x b\n@sealed\n")
        mgr.save_type("myapp", "Unsealed", "1.0", "uint8 a\n")
        typo = mgr.get_type_detail("myapp.Typo.1.0")
        unsealed = mgr.get_type_detail("myapp.Unsealed.1.0")
        assert typo["problem"]["line"] == 2 and typo["problem"]["message"]
        assert unsealed["problem"]["line"] is None and "@sealed" in unsealed["problem"]["message"]
        # What the source says is still there, as written.
        assert [f["name"] for f in typo["fields"]] == ["a", "b"]

    def test_a_problem_in_a_type_it_uses_names_that_type(self, mgr: DsdlManager) -> None:
        mgr.save_type("myapp", "Typo", "1.0", "uint8 a\nuint9x b\n@sealed\n")
        mgr.save_type("myapp", "User", "1.0", "myapp.Typo.1.0 t\n@sealed\n")
        problem = mgr.get_type_detail("myapp.User.1.0")["problem"]
        assert problem["line"] is None and "Typo.1.0.dsdl" in problem["message"]


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
