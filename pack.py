#!/usr/bin/env python3
"""Build the distributable ``.mcdr`` package for Server Log Filter.

Usage::

    python pack.py [output.mcdr]

The packer is deliberately **allowlist-based**. MCDR validates the root entries of a
packed plugin (``PackedPlugin._check_dir_legality``) and refuses to load an archive that
contains a root-level module such as ``conftest.py`` or ``setup.py``. A denylist-style
``rglob("*")`` packer therefore breaks the release as soon as anyone adds a file to the
repository root, and it also sweeps in ``.testlibs/`` (the whole MCDR distribution) once
the test dependencies are installed.

Only these are shipped:

* ``mcdreforged.plugin.json`` — package metadata (required)
* ``server_log_filter/**.py`` — the plugin code, recursively (submodules included)
* ``LICENSE``, ``README.md``, ``README_en.md``, ``CHANGELOG.md`` — documentation

``pack.py`` itself is intentionally **not** included, for the same root-module reason.
"""

import json
import sys
import zipfile
from pathlib import Path

SRC = Path(__file__).resolve().parent

# Root-level files that ship with the plugin.
ROOT_FILES = {
    "mcdreforged.plugin.json",
    "LICENSE",
    "README.md",
    "README_en.md",
    "CHANGELOG.md",
}

# Code package: same name as the plugin id.
PACKAGE_NAME = "server_log_filter"


def plugin_version() -> str:
    with open(SRC / "mcdreforged.plugin.json", encoding="utf-8") as fh:
        return json.load(fh)["version"]


def collect() -> list:
    """Return the sorted list of files to ship."""
    files = []
    for path in SRC.rglob("*"):
        if not path.is_file():
            continue
        if "__pycache__" in path.parts or ".git" in path.parts:
            continue
        if path.suffix == ".pyc":
            continue
        rel = path.relative_to(SRC)
        if rel.parent == Path(".") and rel.name in ROOT_FILES:
            files.append(path)
        elif rel.parts and rel.parts[0] == PACKAGE_NAME and rel.suffix == ".py":
            # Recursive: ``rel.parts[0]`` (not ``rel.parent``) so that submodules
            # such as ``server_log_filter/sub/helper.py`` are shipped too. Using
            # ``rel.parent`` would silently drop them and produce a broken release.
            files.append(path)
    return sorted(files)


def build(out_path: Path) -> Path:
    files = collect()
    if not files:
        raise SystemExit("nothing to pack; is mcdreforged.plugin.json present?")
    mandatory = SRC / "mcdreforged.plugin.json"
    if mandatory not in files:
        raise SystemExit("mcdreforged.plugin.json must be in the package")
    if not any(p.parent == SRC / PACKAGE_NAME for p in files):
        raise SystemExit(f"{PACKAGE_NAME}/ contains no .py files")

    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in files:
            zf.write(path, path.relative_to(SRC).as_posix())
    return out_path


def main() -> int:
    if len(sys.argv) > 1:
        out_path = Path(sys.argv[1]).resolve()
    else:
        out_path = SRC / f"ServerLogFilter-v{plugin_version()}.mcdr"

    build(out_path)
    size_kib = out_path.stat().st_size / 1024
    print(f"packed {len(collect())} files -> {out_path} ({size_kib:.1f} KiB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
