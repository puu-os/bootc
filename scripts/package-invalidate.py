#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) Opinsys Oy 2026

"""Invalidate changed Buildroot packages and their cached consumers."""

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

from package_state import collect

NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.+-]*")


def read_json(path):
    with path.open() as handle:
        return json.load(handle)


def sync_directory(path):
    directory = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(value, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def validate_snapshot(snapshot):
    if (not isinstance(snapshot, dict) or snapshot.get("version") != 1
            or not isinstance(snapshot.get("infrastructure"), str)
            or not isinstance(snapshot.get("external_mk"), str)
            or not isinstance(snapshot.get("packages"), dict)):
        raise ValueError("Invalid package state snapshot")
    for name, package in snapshot["packages"].items():
        if (not NAME.fullmatch(name) or not isinstance(package, dict)
                or not isinstance(package.get("identity"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", package["identity"])
                or not isinstance(package.get("dependencies"), list)
                or not all(isinstance(dep, str) and NAME.fullmatch(dep) for dep in package["dependencies"])
                or not isinstance(package.get("build_dirs"), list)
                or not all(isinstance(path, str) for path in package["build_dirs"])):
            raise ValueError(f"Invalid package state for {name}")


def include_layout(text):
    """Allow dependency-order changes, not arbitrary external Make overrides."""
    if "\0" in text:
        return False
    includes = 0
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("include "):
            expression = line.removeprefix("include ")
            includes += 1
        elif line.startswith("PUU_PYTHON_PACKAGE_MKS := "):
            expression = line.removeprefix("PUU_PYTHON_PACKAGE_MKS := ")
        else:
            return False
        for wildcard in ("*", "python-*"):
            expression = expression.replace(
                f"$(BR2_EXTERNAL_PUU_PATH)/package/{wildcard}/*.mk", "\0")
        expression = expression.replace("$(PUU_PYTHON_PACKAGE_MKS)", "\0")
        expression = re.sub(r"\$\((?:sort|wildcard|filter-out)\s+", "(", expression)
        if "\0" not in expression or not re.fullmatch(r"[\x00(),\s]+", expression):
            return False
    return includes > 0


def board_path(board, relative):
    path = Path(relative)
    if path.is_absolute() or len(path.parts) != 2 or path.parts[0] not in ("build", "per-package"):
        raise ValueError(f"Not a board-owned package directory: {relative}")
    if not NAME.fullmatch(path.parts[1]) or path.parts[1] in (".", ".."):
        raise ValueError(f"Invalid package directory: {relative}")
    result = board / path
    if (board / path.parts[0]).is_symlink() or result.is_symlink():
        raise ValueError(f"Refusing a symlinked package directory: {result}")
    return result


def cached_packages(board):
    packages = board / "per-package"
    return ((packages.is_dir() and any(packages.iterdir()))
            or any(path.parent.name != "buildroot-config" for path in (board / "build").glob("*/.stamp_*"))
            or any(path.is_file() or path.is_symlink() for name in ("target", "host")
                   for path in (board / name).rglob("*")))


def plan(board, current, baseline, journal):
    validate_snapshot(current)
    reference = baseline
    if journal is not None:
        if (not isinstance(journal, dict) or journal.get("version") != 1
                or journal.get("phase") not in ("invalidating", "prepared")
                or not isinstance(journal.get("dirty"), list)
                or not all(isinstance(name, str) and NAME.fullmatch(name) for name in journal["dirty"])
                or not isinstance(journal.get("paths"), list)
                or not all(isinstance(path, str) for path in journal["paths"])):
            raise ValueError("Invalid package invalidation journal")
        validate_snapshot(journal["snapshot"])
        if journal["phase"] == "prepared":
            reference = journal["snapshot"]
    snapshots = [snapshot for snapshot in (baseline, reference, current) if snapshot is not None]
    if journal is not None:
        snapshots.append(journal["snapshot"])
    for snapshot in snapshots:
        validate_snapshot(snapshot)
        if snapshot["external_mk"] != current["external_mk"] and not (
                include_layout(snapshot["external_mk"]) and include_layout(current["external_mk"])):
            raise ValueError("Unclassified external.mk change; refusing to reuse cached packages")
    previous = reference["packages"] if reference is not None else {}
    packages = current["packages"]
    dirty = {name for name in previous.keys() | packages.keys()
             if previous.get(name) != packages.get(name)}
    if reference is not None and reference["infrastructure"] != current["infrastructure"]:
        dirty.update(name for snapshot in snapshots for name in snapshot["packages"])
    paths = set()
    if journal is not None and journal["phase"] == "invalidating":
        dirty.update(journal["dirty"])
        paths.update(journal["paths"])
    reverse = {}
    for snapshot in snapshots:
        for name, package in snapshot["packages"].items():
            for dependency in package["dependencies"]:
                reverse.setdefault(dependency, set()).add(name)
    queue = list(dirty)
    while queue:
        for consumer in reverse.get(queue.pop(), ()):
            if consumer not in dirty:
                dirty.add(consumer)
                queue.append(consumer)
    for name in dirty:
        paths.add(f"per-package/{name}")
        for snapshot in snapshots:
            package = snapshot["packages"].get(name)
            if package is not None:
                paths.update(package["build_dirs"])
    for path in paths:
        board_path(board, path)
    return dirty, paths


def state_paths(board):
    directory = board / "stamp/orchestration"
    return directory, directory / "packages.json", directory / "packages.pending.json"


def prepare(board, current, *, readonly=False):
    directory, baseline_path, journal_path = state_paths(board)
    baseline = read_json(baseline_path) if baseline_path.exists() else None
    journal = read_json(journal_path) if journal_path.exists() else None
    if baseline is None and journal is None and cached_packages(board):
        raise ValueError("Cached packages have no verified package metadata; seed their known successful baseline first")
    dirty, paths = plan(board, current, baseline, journal)
    cached = sorted(name for name in dirty if (board / "per-package" / name).exists()
                    or any(board_path(board, path).exists() for snapshot in (baseline, current)
                           if snapshot is not None for path in snapshot["packages"].get(name, {}).get("build_dirs", [])))
    if readonly:
        print(json.dumps({"dirty": sorted(dirty), "cached": cached, "paths": sorted(paths)}, sort_keys=True))
        return
    write_json(directory / "packages.next.json", current)
    if not dirty and journal is None:
        return
    if dirty:
        print(f"{board.name}: invalidating {len(cached)} cached packages: {' '.join(cached)}", flush=True)
        pending = {
            "version": 1, "phase": "invalidating", "snapshot": current,
            "dirty": sorted(dirty),
            "paths": sorted(paths),
        }
        write_json(journal_path, pending)
        (directory / "build.json").unlink(missing_ok=True)
        for relative in sorted(paths):
            path = board_path(board, relative)
            if path.exists():
                shutil.rmtree(path)
        for path in (board / "build", board / "per-package", board):
            if path.exists():
                sync_directory(path)
        pending["phase"] = "prepared"
        write_json(journal_path, pending)
    else:
        journal["snapshot"] = current
        write_json(journal_path, journal)
        (directory / "build.json").unlink(missing_ok=True)


def assemble(board):
    """Recreate aggregates on every real build, including image-only changes."""
    directory, _, journal_path = state_paths(board)
    current = read_json(directory / "packages.next.json")
    validate_snapshot(current)
    if journal_path.exists():
        journal = read_json(journal_path)
        if journal.get("phase") != "prepared" or journal.get("snapshot") != current:
            raise ValueError("Package invalidation must finish before aggregate assembly")
    else:
        write_json(journal_path, {"version": 1, "phase": "prepared", "snapshot": current,
                                  "dirty": [], "paths": []})
    (directory / "build.json").unlink(missing_ok=True)
    for name in ("target", "host"):
        path = board / name
        if path.is_symlink():
            raise ValueError(f"Refusing a symlinked aggregate directory: {path}")
        if path.exists():
            shutil.rmtree(path)
    sync_directory(board)


def commit(board, current):
    directory, baseline_path, journal_path = state_paths(board)
    expected = read_json(directory / "packages.next.json")
    validate_snapshot(current)
    if current != expected:
        raise ValueError("Package inputs changed during the build; not recording success")
    if journal_path.exists():
        journal = read_json(journal_path)
        if journal.get("phase") != "prepared" or journal.get("snapshot") != current:
            raise ValueError("Package invalidation is not ready to commit")
    write_json(baseline_path, current)
    (directory / "packages.next.json").unlink()
    journal_path.unlink(missing_ok=True)
    sync_directory(directory)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("prepare", "plan", "commit"):
        command = subparsers.add_parser(name)
        command.add_argument("--root", required=True, type=Path)
        command.add_argument("--board", required=True, type=Path)
        command.add_argument("--source", required=True, type=Path)
        command.add_argument("make_args", nargs=argparse.REMAINDER)
    command = subparsers.add_parser("assemble")
    command.add_argument("--board", required=True, type=Path)
    args = parser.parse_args()
    board = args.board.resolve()
    if not board.is_dir() or not (board / ".config").is_file():
        parser.error("board must be an existing configured Buildroot output")
    try:
        if args.command == "assemble":
            assemble(board)
        else:
            make_args = args.make_args[1:] if args.make_args[:1] == ["--"] else args.make_args
            snapshot = collect(args.root.resolve(), board, args.source.resolve(), make_args)
            if args.command == "commit":
                commit(board, snapshot)
            else:
                prepare(board, snapshot, readonly=args.command == "plan")
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        print(f"package-state: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
