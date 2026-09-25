"""DSDL introspection for Cynitor.

Walks DSDL source directories, parses type definitions,
and reports compilation status. Independent of CAN connection.
"""

import importlib
import logging
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_FIELD_RE = re.compile(
    r"^(?:truncated\s+|saturated\s+)?"
    r"(?P<type>\S+)"
    r"\s+"
    r"(?P<name>[a-zA-Z_]\w*)"
    r"(?:\s*=\s*(?P<value>[^#]+))?"
)


class DsdlManager:

    def __init__(self, project_root: Path | str, data_dir: Optional[Path | str] = None) -> None:
        """``data_dir`` is where custom types and their compiled code are kept.

        Without it, both live in the source tree, as they used to. The server
        passes its data folder, because the source tree is not writable where
        it matters: inside the single-file executable it is a temporary
        folder, emptied on every exit.
        """
        self.project_root = Path(project_root)
        self.dsdl_dir = self.project_root / "dsdl_messages"
        self.public_types_dir = self.dsdl_dir / "public_regulated_data_types"
        # The public regulated types, compiled once (and built into the executable).
        self.compiled_dir = self.project_root / "python_compiled_messages"
        legacy_custom_dir = self.dsdl_dir / "custom"
        if data_dir is None:
            self.custom_dir = legacy_custom_dir
            self.custom_compiled_dir = self.compiled_dir
        else:
            self.custom_dir = Path(data_dir) / "dsdl" / "custom"
            self.custom_compiled_dir = Path(data_dir) / "dsdl" / "compiled"
            self._adopt_legacy_custom_types(legacy_custom_dir)
        # Public types cannot be recompiled into the executable's temporary folder.
        self.public_compilable = not getattr(sys, "frozen", False)

        self._tree_cache: Optional[dict] = None
        self._type_index: dict[str, Path] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def make_importable(self) -> None:
        """Let compiled custom types be imported, including ones compiled in earlier runs."""
        path = str(self.custom_compiled_dir.resolve())
        if path not in sys.path:
            sys.path.append(path)

    def get_status(self) -> dict[str, Any]:
        paths: list[dict] = []
        if self.public_types_dir.is_dir():
            paths.append({"path": str(self.public_types_dir), "label": "Public regulated types", "source": "regulated"})
        if self.custom_dir.is_dir():
            paths.append({"path": str(self.custom_dir), "label": "Custom types", "source": "custom"})

        compiled_ok = all((self.compiled_dir / ns).is_dir() for ns in ("uavcan", "reg"))

        public_roots = {"uavcan", "reg"}
        last_public_compiled = self._max_compiled_mtime(
            lambda p: p.parts and p.parts[0] in public_roots
        ) if compiled_ok else None
        last_custom_compiled = self._max_compiled_mtime(
            lambda p: p.parts and p.parts[0] not in public_roots,
            self.custom_compiled_dir,
        )
        last_compiled_candidates = [t for t in (last_public_compiled, last_custom_compiled) if t is not None]
        last_compiled = max(last_compiled_candidates) if last_compiled_candidates else None

        source_count = sum(1 for _ in self.public_types_dir.rglob("*.dsdl")) if self.public_types_dir.is_dir() else 0
        custom_count = sum(1 for _ in self.custom_dir.rglob("*.dsdl")) if self.custom_dir.is_dir() else 0

        return {
            "paths": paths,
            "public_compilable": self.public_compilable,
            "compiled": compiled_ok,
            "last_compiled": last_compiled,
            "last_public_compiled": last_public_compiled,
            "last_custom_compiled": last_custom_compiled,
            "source_types": source_count,
            "custom_types": custom_count,
        }

    def _max_compiled_mtime(self, predicate, root: Optional[Path] = None) -> Optional[float]:
        root = self.compiled_dir if root is None else root
        if not root.is_dir():
            return None
        try:
            mtimes = []
            for f in root.rglob("*.py"):
                if f.name.startswith("__") or f.name == "nunavut_support.py":
                    continue
                rel = f.relative_to(root)
                if predicate(rel):
                    mtimes.append(f.stat().st_mtime)
            return max(mtimes) if mtimes else None
        except (ValueError, OSError):
            return None

    def get_namespaces(self) -> dict[str, Any]:
        if self._tree_cache is not None:
            return self._tree_cache

        tree: dict[str, Any] = {}
        self._type_index.clear()

        if self.public_types_dir.is_dir():
            for ns_root in ("uavcan", "reg"):
                ns_dir = self.public_types_dir / ns_root
                if ns_dir.is_dir():
                    self._walk_namespace(ns_dir, ns_root, tree, "regulated")

        if self.custom_dir.is_dir():
            for child in sorted(self.custom_dir.iterdir()):
                if child.is_dir() and not child.name.startswith("."):
                    self._walk_namespace(child, child.name, tree, "custom")
                    self._ensure_custom_dirs(child, child.name, tree)

        self._tree_cache = {"namespaces": tree}
        return self._tree_cache

    def get_type_detail(self, full_name: str) -> Optional[dict[str, Any]]:
        if not self._type_index:
            self.get_namespaces()

        path = self._type_index.get(full_name)
        if path is None:
            return None

        parsed = self._parse_source(path)
        parts = full_name.split(".")
        version = f"{parts[-2]}.{parts[-1]}"
        short_name = parts[-3]
        namespace = ".".join(parts[:-3])

        _, _, fixed_port_id = self._parse_filename(path.name)
        is_custom = self.custom_dir.is_dir() and str(path).startswith(str(self.custom_dir))

        return {
            "full_name": full_name,
            "namespace": namespace,
            "short_name": short_name,
            "version": version,
            "kind": parsed["kind"],
            "fixed_port_id": fixed_port_id,
            "source": "custom" if is_custom else "regulated",
            "source_file": str(path),
            "source_text": parsed["source_text"],
            "fields": parsed["fields"],
            "constants": parsed["constants"],
            "dependencies": self._resolve_dependencies(parsed["dependencies"], namespace),
            "compiled": self._is_compiled(full_name),
        }

    def invalidate_cache(self) -> None:
        self._tree_cache = None
        self._type_index.clear()

    def create_namespace(self, namespace: str) -> dict[str, Any]:
        self._validate_namespace(namespace)
        ns_dir = self.custom_dir / Path(*namespace.split("."))
        if ns_dir.is_dir():
            raise ValueError(f"Namespace '{namespace}' already exists")
        ns_dir.mkdir(parents=True, exist_ok=True)
        self.invalidate_cache()
        return {"namespace": namespace, "path": str(ns_dir)}

    def save_type(self, namespace: str, type_name: str, version: str,
                  source_text: str, fixed_port_id: Optional[int] = None,
                  overwrite: bool = False) -> dict[str, Any]:
        self._validate_namespace(namespace)
        if not re.match(r"^[A-Z][A-Za-z0-9_]*$", type_name):
            raise ValueError("Type name must start with uppercase letter and contain only alphanumeric/underscore")
        if not re.match(r"^\d+\.\d+$", version):
            raise ValueError("Version must be MAJOR.MINOR (e.g. 1.0)")

        ns_dir = self.custom_dir / Path(*namespace.split("."))
        ns_dir.mkdir(parents=True, exist_ok=True)

        prefix = f"{fixed_port_id}." if fixed_port_id is not None else ""
        filename = f"{prefix}{type_name}.{version}.dsdl"
        file_path = ns_dir / filename

        existing = list(ns_dir.glob(f"*.{type_name}.{version}.dsdl")) + \
                   list(ns_dir.glob(f"{type_name}.{version}.dsdl"))

        if existing and not overwrite:
            raise ValueError(f"Type '{namespace}.{type_name}.{version}' already exists")

        for old in existing:
            if old != file_path:
                old.unlink()

        file_path.write_text(source_text, encoding="utf-8")
        self.invalidate_cache()

        full_name = f"{namespace}.{type_name}.{version}"
        return {"full_name": full_name, "path": str(file_path)}

    def delete_type(self, namespace: str, type_name: str, version: str) -> dict[str, Any]:
        self._validate_namespace(namespace)
        if not re.match(r"^[A-Z][A-Za-z0-9_]*$", type_name):
            raise ValueError("Type name must start with uppercase letter and contain only alphanumeric/underscore")
        if not re.match(r"^\d+\.\d+$", version):
            raise ValueError("Version must be MAJOR.MINOR (e.g. 1.0)")

        ns_dir = self.custom_dir / Path(*namespace.split("."))
        matches = list(ns_dir.glob(f"*.{type_name}.{version}.dsdl")) + \
                  list(ns_dir.glob(f"{type_name}.{version}.dsdl"))
        if not matches:
            raise FileNotFoundError(f"Type not found: {namespace}.{type_name}.{version}")

        for f in matches:
            f.unlink()

        # Also remove any compiled output for this type so it disappears from
        # the runtime in step with the source. The compiled .py and its pycache
        # entry are produced by nunavut at <compiled_dir>/<ns_path>/<Type>_<MAJOR>_<MINOR>.{py,pyc}.
        # We deliberately leave the namespace's __init__.py alone — recompiling
        # the namespace regenerates it cleanly; trying to patch it here is too
        # risky if other types share the namespace.
        major, minor = version.split(".")
        compiled_ns_dir = self.custom_compiled_dir / Path(*namespace.split("."))
        compiled_basename = f"{type_name}_{major}_{minor}"
        for stem_path in (
            compiled_ns_dir / f"{compiled_basename}.py",
            compiled_ns_dir / "__pycache__" / f"{compiled_basename}.cpython-310.pyc",
        ):
            try:
                if stem_path.is_file():
                    stem_path.unlink()
            except OSError as exc:
                logger.warning("Failed to remove compiled artifact %s: %s", stem_path, exc)
        # Catch any other pycache variants (different Python versions) with a glob
        pyc_glob = compiled_ns_dir / "__pycache__"
        if pyc_glob.is_dir():
            for pyc in pyc_glob.glob(f"{compiled_basename}.cpython-*.pyc"):
                try:
                    pyc.unlink()
                except OSError as exc:
                    logger.warning("Failed to remove %s: %s", pyc, exc)

        self.invalidate_cache()
        self._refresh_python_module_cache()

        full_name = f"{namespace}.{type_name}.{version}"
        return {"full_name": full_name, "deleted": True}

    def is_compiled(self, full_name: str) -> bool:
        return self._is_compiled(full_name)

    def compile_custom(self) -> dict[str, Any]:
        if not self.custom_dir.is_dir() or not any(self.custom_dir.rglob("*.dsdl")):
            return {"ok": False, "error": "No custom types to compile"}
        return self._run_compilation(scope="custom")

    def compile_public(self) -> dict[str, Any]:
        if not self.public_compilable:
            return {"ok": False, "error": "The public regulated types are built into this executable"}
        return self._run_compilation(scope="public")

    def compile_all(self) -> dict[str, Any]:
        return self._run_compilation(scope="all")

    def list_custom_namespaces(self) -> list[str]:
        if not self.custom_dir.is_dir():
            return []
        namespaces = []
        for path in sorted(self.custom_dir.rglob("*")):
            if path.is_dir() and not path.name.startswith("."):
                rel = path.relative_to(self.custom_dir)
                namespaces.append(".".join(rel.parts))
        return namespaces

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _walk_namespace(self, base_dir: Path, ns_prefix: str, tree: dict, source: str) -> None:
        for dsdl_file in sorted(base_dir.rglob("*.dsdl")):
            rel = dsdl_file.relative_to(base_dir)
            ns_parts = list(rel.parent.parts)

            type_name, version, fixed_port_id = self._parse_filename(dsdl_file.name)
            if type_name is None:
                continue

            full_ns = ".".join([ns_prefix] + ns_parts) if ns_parts else ns_prefix
            full_name = f"{full_ns}.{type_name}.{version}"
            kind, field_names = self._quick_parse(dsdl_file)
            self._type_index[full_name] = dsdl_file

            if ns_prefix not in tree:
                tree[ns_prefix] = {"children": {}, "types": []}
            current = tree[ns_prefix]
            for part in ns_parts:
                if part not in current["children"]:
                    current["children"][part] = {"children": {}, "types": []}
                current = current["children"][part]

            current["types"].append({
                "short_name": type_name,
                "full_name": full_name,
                "version": version,
                "kind": kind,
                "fixed_port_id": fixed_port_id,
                "source": source,
                "field_names": field_names,
                "compiled": self._is_compiled(full_name),
            })

    def _ensure_custom_dirs(self, base_dir: Path, ns_prefix: str, tree: dict) -> None:
        """Ensure empty custom directories appear in the tree."""
        if ns_prefix not in tree:
            tree[ns_prefix] = {"children": {}, "types": [], "_source": "custom"}
        node = tree[ns_prefix]
        node["_source"] = "custom"
        self._ensure_custom_children(base_dir, node)

    def _ensure_custom_children(self, base_dir: Path, node: dict) -> None:
        for sub in sorted(base_dir.iterdir()):
            if sub.is_dir() and not sub.name.startswith("."):
                if sub.name not in node["children"]:
                    node["children"][sub.name] = {"children": {}, "types": [], "_source": "custom"}
                child = node["children"][sub.name]
                child["_source"] = "custom"
                self._ensure_custom_children(sub, child)

    @staticmethod
    def _parse_filename(filename: str) -> tuple[Optional[str], Optional[str], Optional[int]]:
        name = filename[:-5] if filename.endswith(".dsdl") else filename
        parts = name.split(".")
        if len(parts) < 3:
            return None, None, None
        major, minor = parts[-2], parts[-1]
        if not major.isdigit() or not minor.isdigit():
            return None, None, None
        fixed_port_id = int(parts[0]) if parts[0].isdigit() else None
        start = 1 if fixed_port_id is not None else 0
        type_name = ".".join(parts[start:-2])
        return type_name, f"{major}.{minor}", fixed_port_id

    @staticmethod
    def _quick_parse(path: Path) -> tuple[str, list[str]]:
        """Single-pass scan: returns (kind, field_names)."""
        kind = "message"
        field_names: list[str] = []
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return kind, field_names
        for line in text.split("\n"):
            stripped = line.strip()
            if stripped == "---":
                kind = "service"
                continue
            if not stripped or stripped.startswith("#") or stripped.startswith("@"):
                continue
            m = _FIELD_RE.match(stripped)
            if not m or m.group("value") is not None:
                continue
            if m.group("type").startswith("void"):
                continue
            field_names.append(m.group("name"))
        return kind, field_names

    def _resolve_dependencies(self, raw_deps: list[str], namespace: str) -> list[str]:
        """Resolve relative dependency names to full type names."""
        if not self._type_index:
            self.get_namespaces()
        resolved = []
        for dep in raw_deps:
            full = self._resolve_one_dep(dep, namespace)
            resolved.append(full)
        return resolved

    def _resolve_one_dep(self, dep: str, namespace: str) -> str:
        if dep in self._type_index:
            return dep
        parts = namespace.split(".")
        for i in range(len(parts), 0, -1):
            candidate = ".".join(parts[:i]) + "." + dep
            if candidate in self._type_index:
                return candidate
        return dep

    def _parse_source(self, path: Path) -> dict[str, Any]:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return {"kind": "message", "fields": [], "constants": [], "dependencies": [], "source_text": ""}

        is_service = False
        current_section = "message"
        fields: dict[str, list] = {"message": []}
        constants: list[dict] = []
        dependencies: set[str] = set()

        for line in text.split("\n"):
            stripped = line.strip()
            if stripped == "---":
                is_service = True
                fields["request"] = fields.pop("message", [])
                fields["response"] = []
                current_section = "response"
                continue
            if not stripped or stripped.startswith("#") or stripped.startswith("@"):
                continue

            m = _FIELD_RE.match(stripped)
            if not m:
                continue

            field_type = m.group("type")
            field_name = m.group("name")
            const_value = m.group("value")

            if const_value is not None:
                constants.append({"name": field_name, "type": field_type, "value": const_value.strip()})
                continue
            if field_type.startswith("void"):
                continue

            if current_section not in fields:
                fields[current_section] = []
            fields[current_section].append({"name": field_name, "type": field_type})

            base_type = re.sub(r"\[.*\]", "", field_type)
            if "." in base_type:
                dependencies.add(base_type)

        if is_service:
            result_fields: Any = {"request": fields.get("request", []), "response": fields.get("response", [])}
        else:
            result_fields = fields.get("message", [])

        return {
            "kind": "service" if is_service else "message",
            "fields": result_fields,
            "constants": constants,
            "dependencies": sorted(dependencies),
            "source_text": text,
        }

    def _is_compiled(self, full_name: str) -> bool:
        parts = full_name.split(".")
        if len(parts) < 4:
            return False
        compiled_name = f"{parts[-3]}_{parts[-2]}_{parts[-1]}.py"
        relative = Path(*parts[:-3]) / compiled_name
        return any((root / relative).is_file() for root in (self.compiled_dir, self.custom_compiled_dir))

    @staticmethod
    def _validate_namespace(namespace: str) -> None:
        if not namespace or not re.match(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$", namespace):
            raise ValueError("Namespace must be lowercase dotted identifiers (e.g. myapp.sensors)")

    def _run_compilation(self, scope: str = "all") -> dict[str, Any]:
        uavcan_dir = self.public_types_dir / "uavcan"
        reg_dir = self.public_types_dir / "reg"
        errors: list[str] = []

        # "all" in the executable means the custom types: the public ones are built in.
        if scope in ("all", "public") and self.public_compilable:
            errors += self._compile(reg_dir, [uavcan_dir], self.compiled_dir, "reg")
            errors += self._compile(uavcan_dir, [reg_dir], self.compiled_dir, "uavcan")

        if scope in ("all", "custom") and self.custom_dir.is_dir():
            custom_roots = [c for c in sorted(self.custom_dir.iterdir())
                            if c.is_dir() and not c.name.startswith(".")]
            for child in custom_roots:
                siblings = [c for c in custom_roots if c != child]
                errors += self._compile(
                    child,
                    [uavcan_dir, reg_dir, *siblings],
                    self.custom_compiled_dir,
                    f"custom/{child.name}",
                )

        self.invalidate_cache()
        if errors:
            return {"ok": False, "errors": errors}
        # Drop Python's cached module objects for any namespace under
        # compiled_dir so the scanner's next import_module() picks up the
        # freshly generated .py files instead of the pre-compile snapshot
        # already in sys.modules.
        self._refresh_python_module_cache()
        return {"ok": True}

    def _refresh_python_module_cache(self) -> None:
        roots = [r for r in {self.compiled_dir, self.custom_compiled_dir} if r.is_dir()]
        if not roots:
            return
        importlib.invalidate_caches()
        compiled_top_namespaces = {
            p.name for root in roots for p in root.iterdir()
            if p.is_dir() and not p.name.startswith("_") and not p.name.startswith(".")
        }
        if not compiled_top_namespaces:
            return
        for module_name in list(sys.modules):
            top = module_name.split(".", 1)[0]
            if top in compiled_top_namespaces:
                sys.modules.pop(module_name, None)

    @staticmethod
    def _compile(target: Path, lookups: list[Path], output: Path, label: str) -> list[str]:
        """Compile the namespace at ``target`` into ``output``; return error messages.

        In-process through pycyphal (which drives nunavut), not by running
        nnvg: the executable bundles the libraries but has no nnvg to run.
        """
        # pydsdl and nunavut log a line per type at INFO: hundreds for the
        # public types, which would bury the dashboard's log panel.
        chatty = [logging.getLogger(name) for name in ("pydsdl", "nunavut")]
        levels = [lg.level for lg in chatty]
        for lg in chatty:
            lg.setLevel(logging.WARNING)
        try:
            import pycyphal.dsdl
            output.mkdir(parents=True, exist_ok=True)
            pycyphal.dsdl.compile(target, [ld for ld in lookups if ld.is_dir()], output_directory=output)
        except Exception as exc:
            return [f"{label}: {exc}"]
        finally:
            for lg, level in zip(chatty, levels):
                lg.setLevel(level)
        return []

    def _adopt_legacy_custom_types(self, legacy_dir: Path) -> None:
        """Copy custom types an earlier version kept in the source tree into the data folder.

        Copied, not moved: in a source checkout they may be under version
        control. Only when the data folder has no custom types yet.
        """
        if self.custom_dir.exists() or not legacy_dir.is_dir() or not any(legacy_dir.rglob("*.dsdl")):
            return
        try:
            shutil.copytree(legacy_dir, self.custom_dir)
        except OSError as exc:
            logger.warning("Could not copy custom DSDL types from %s: %s", legacy_dir, exc)
            return
        logger.info("Copied custom DSDL types from %s to %s; compile them there once", legacy_dir, self.custom_dir)
