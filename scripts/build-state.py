#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) Opinsys Oy 2026

"""Content identities and successful-output records for the build scripts."""

import argparse
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile


def fingerprint(paths, values=(), excludes=(), metadata=False):
    digest = hashlib.sha256()

    def record(value):
        digest.update(json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode())
        digest.update(b"\n")

    def visit(path, relative):
        if any(fnmatch.fnmatch(relative, pattern) or fnmatch.fnmatch(path.name, pattern)
               for pattern in excludes):
            return
        info = path.lstat()
        record([relative, stat.S_IMODE(info.st_mode)])
        if metadata:
            record([info.st_dev, info.st_ino, info.st_size, info.st_uid, info.st_gid,
                    info.st_mtime_ns, info.st_ctime_ns])
        if stat.S_ISLNK(info.st_mode):
            record(["link", os.readlink(path)])
        elif stat.S_ISDIR(info.st_mode):
            record("directory")
            for child in sorted(path.iterdir()):
                visit(child, f"{relative}/{child.name}")
        elif stat.S_ISREG(info.st_mode):
            record(["file", info.st_size])
            if not metadata:
                with path.open("rb") as handle:
                    while block := handle.read(1024 * 1024):
                        digest.update(block)
                digest.update(b"\n")
        else:
            raise ValueError(f"unsupported build input: {path}")

    record(["build-state", 1])
    for value in values:
        record(["value", value])
    for name in paths:
        path = Path(name)
        label = os.path.normpath(name)
        record(["path", label])
        visit(path, label)
    return digest.hexdigest()


def build_tree_key(board):
    """Detect manual package operations and removed dependency snapshots cheaply."""
    entries = []

    def stat_entry(label, path):
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            entries.append([label, "missing"])
        else:
            entries.append([label, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns])

    for rel in ("target", "target/usr", "target/etc", "host", "host/bin", "staging"):
        stat_entry(rel, board / rel)

    for tree in ("build", "per-package"):
        root = board / tree
        if not root.is_dir():
            entries.append([tree, "missing"])
            continue
        with os.scandir(root) as packages:
            package_entries = sorted(
                (entry for entry in packages if entry.is_dir(follow_symlinks=False)),
                key=lambda entry: entry.name,
            )
        for package in package_entries:
            prefix = f"{tree}/{package.name}"
            stat_entry(prefix, package.path)
            if tree == "build":
                with os.scandir(package.path) as children:
                    stamps = sorted(
                        (child for child in children if child.name.startswith(".stamp")),
                        key=lambda child: child.name,
                    )
                for stamp in stamps:
                    stat_entry(f"{prefix}/{stamp.name}", stamp.path)
            else:
                for sub in ("host", "target", "staging"):
                    stat_entry(f"{prefix}/{sub}", os.path.join(package.path, sub))
    return hashlib.sha256(json.dumps(entries, separators=(",", ":")).encode()).hexdigest()


def save_state(path, key, outputs, metadata_outputs):
    for output in outputs + metadata_outputs:
        if not Path(output).exists():
            raise FileNotFoundError(f"missing build output: {output}")
    data = {
        "version": 1,
        "key": key,
        "outputs": fingerprint(outputs),
        "metadata_outputs": fingerprint(metadata_outputs, metadata=True),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(data, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    key_parser = subparsers.add_parser("key")
    key_parser.add_argument("--value", action="append", default=[])
    key_parser.add_argument("--path", action="append", default=[])
    key_parser.add_argument("--exclude", action="append", default=[])
    tree_parser = subparsers.add_parser("build-tree")
    tree_parser.add_argument("board", type=Path)
    for command in ("check", "save"):
        command_parser = subparsers.add_parser(command)
        command_parser.add_argument("state", type=Path)
        command_parser.add_argument("--key", required=True)
        command_parser.add_argument("--path", action="append", default=[])
        command_parser.add_argument("--metadata-path", action="append", default=[])
    args = parser.parse_args()
    if args.command in ("check", "save") and not (args.path or args.metadata_path):
        parser.error(f"{args.command} requires at least one --path or --metadata-path")
    try:
        if args.command == "key":
            print(fingerprint(args.path, args.value, args.exclude))
        elif args.command == "build-tree":
            print(build_tree_key(args.board))
        elif args.command == "save":
            save_state(args.state, args.key, args.path, args.metadata_path)
        else:
            try:
                data = json.loads(args.state.read_text())
            except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError):
                return 1
            if not isinstance(data, dict) or data.get("version") != 1 or data.get("key") != args.key:
                return 1
            if any(not Path(path).exists() for path in args.path + args.metadata_path):
                return 1
            if data.get("outputs") != fingerprint(args.path):
                return 1
            if data.get("metadata_outputs") != fingerprint(args.metadata_path, metadata=True):
                return 1
    except (OSError, ValueError) as error:
        print(f"build-state: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
