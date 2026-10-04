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
* ``server_log_filter/lang/*.json`` — the message catalogues (one file per language)
* ``LICENSE``, ``CHANGELOG.md`` — licence text and the shipped changelog

``README.md`` / ``README_en.md`` are deliberately **excluded**: they are long, they
duplicate what the release page already says, and MCDR never reads them. Keeping
them out cuts roughly half off the artifact. ``server_log_filter/lang/README.md``
— the contributor guide for translators — is excluded for the same reason.

``pack.py`` itself is intentionally **not** included, for the same root-module reason.
"""

import json
import sys
import zipfile
from pathlib import Path

SRC = Path(__file__).resolve().parent

# Root-level files that ship with the plugin.
#
# README.md / README_en.md are intentionally absent: MCDR never reads them, they
# duplicate the release page, and together they were over half the artifact size.
ROOT_FILES = {
    "mcdreforged.plugin.json",
    "LICENSE",
    "CHANGELOG.md",
}

# Code package: same name as the plugin id.
PACKAGE_NAME = "server_log_filter"

# Data inside the package that ships alongside the code. The message catalogues must
# travel with the plugin — without them every message degrades to its raw key — and
# keeping them as separate .json files is what lets a translator add a language
# without touching Python.
PACKAGE_DATA_DIRS = {
    "lang": (".json",),
}


def _is_package_payload(rel: Path) -> bool:
    """True for files inside the plugin package that belong in the artifact."""
    if rel.parts[0] != PACKAGE_NAME:
        return False
    if rel.suffix == ".py":
        # Every module, at any depth: a silently dropped submodule would produce an
        # artifact that imports fine here and explodes on the user's machine.
        return True
    # A data directory such as ``lang/``: only the extensions we asked for, and only
    # directly inside it. ``lang/README.md`` (the translator guide) stays out.
    if len(rel.parts) == 3 and rel.parts[1] in PACKAGE_DATA_DIRS:
        return rel.suffix in PACKAGE_DATA_DIRS[rel.parts[1]]
    return False


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
        elif rel.parts and _is_package_payload(rel):
            # Recursive by design: checking ``rel.parts[0]`` (not ``rel.parent``) is what
            # lets ``server_log_filter/sub/helper.py`` and ``server_log_filter/lang/x.json``
            # ship too. Using ``rel.parent`` would silently drop them and produce a
            # broken release.
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
