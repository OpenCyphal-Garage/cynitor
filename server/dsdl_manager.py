"""DSDL introspection for Cynitor.

Walks DSDL source directories, parses type definitions,
and reports compilation status. Independent of CAN connection.
"""

import logging
import re
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

    def __init__(self, project_root: Path | str) -> None:
        self.project_root = Path(project_root)
        self.dsdl_dir = self.project_root / "dsdl_messages"
        self.public_types_dir = self.dsdl_dir / "public_regulated_data_types"
        self.compiled_dir = self.project_root / "python_compiled_messages"
        self.custom_dir = self.dsdl_dir / "custom"

        self._tree_cache: Optional[dict] = None
        self._type_index: dict[str, Path] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_status(self) -> dict[str, Any]:
        paths: list[dict] = []
        if self.public_types_dir.is_dir():
            paths.append({"path": str(self.public_types_dir), "label": "Public regulated types", "source": "regulated"})
        if self.custom_dir.is_dir():
            paths.append({"path": str(self.custom_dir), "label": "Custom types", "source": "custom"})

        compiled_ok = all((self.compiled_dir / ns).is_dir() for ns in ("uavcan", "reg"))

        last_compiled: Optional[float] = None
        if compiled_ok:
            try:
                last_compiled = max(
                    f.stat().st_mtime for f in self.compiled_dir.rglob("*.py") if not f.name.startswith("__")
                )
            except (ValueError, OSError):
                pass

        source_count = sum(1 for _ in self.public_types_dir.rglob("*.dsdl")) if self.public_types_dir.is_dir() else 0
        custom_count = sum(1 for _ in self.custom_dir.rglob("*.dsdl")) if self.custom_dir.is_dir() else 0

        return {
            "paths": paths,
            "compiled": compiled_ok,
            "last_compiled": last_compiled,
            "source_types": source_count,
            "custom_types": custom_count,
        }

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
            })

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
        compiled_path = self.compiled_dir / Path(*parts[:-3]) / compiled_name
        return compiled_path.is_file()
