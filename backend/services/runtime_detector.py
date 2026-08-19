"""Canonical project runtime detection for deployment.

Detection is based on project files, not host capabilities. Host capabilities
only decide whether an already-classified dynamic runtime can be started.
"""

from __future__ import annotations

import ast
import json
from enum import Enum
from typing import Mapping


class RuntimeType(str, Enum):
    STATIC = "static"
    PYTHON = "python"
    NODE = "node"


class RuntimeDetectionError(ValueError):
    """Raised when project files do not identify a safe runtime."""


def _valid_python(source: str) -> bool:
    try:
        ast.parse(source or "")
    except (SyntaxError, ValueError, TypeError):
        return False
    return bool((source or "").strip())


def _node_manifest_is_server(manifest: object) -> bool:
    if not isinstance(manifest, dict):
        return False
    scripts = manifest.get("scripts")
    if isinstance(scripts, dict) and any(
        key in scripts for key in ("start", "serve", "dev")
    ):
        return True
    return bool(manifest.get("main") or manifest.get("bin"))


def detect_runtime(files: Mapping[str, str]) -> RuntimeType:
    """Return the only safe runtime implied by a workspace file snapshot.

    Precedence is: explicit server manifest, valid Python project, static site.
    ``app.js`` alone is a browser asset and never makes a project Node. A
    malformed ``main.py`` is ignored; an ``index.html`` project therefore stays
    static rather than being forced into Python.
    """
    normalized = {
        str(path).replace("\\", "/").lstrip("./"): str(content or "")
        for path, content in files.items()
    }
    package_raw = normalized.get("package.json")
    if package_raw:
        try:
            package = json.loads(package_raw)
        except json.JSONDecodeError as exc:
            raise RuntimeDetectionError("package.json is invalid") from exc
        if _node_manifest_is_server(package):
            return RuntimeType.NODE

    python_entry = next(
        (normalized[name] for name in ("main.py", "app.py", "run.py") if name in normalized),
        None,
    )
    python_manifest = any(name in normalized for name in ("requirements.txt", "pyproject.toml"))
    index_html = normalized.get("index.html", "").strip()
    is_static = bool(
        index_html
        and ("<html" in index_html.lower() or "<!doctype html" in index_html.lower())
    )
    if python_entry is not None and _valid_python(python_entry):
        return RuntimeType.PYTHON
    if is_static:
        return RuntimeType.STATIC
    if python_manifest and python_entry is not None:
        raise RuntimeDetectionError("Python manifest has no valid Python entrypoint")

    raise RuntimeDetectionError("Unable to determine a safe project runtime")
