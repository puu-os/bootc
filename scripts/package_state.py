#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) Opinsys Oy 2026

"""Collect versioned Buildroot package identities, without invalidating packages.

Only Buildroot's existing show-info goal is invoked. Buildroot itself may
regenerate .br2-external files while parsing; callers must serialize the O tree.
No previous state (including legacy hash-only state) is read or migrated here.
"""

import argparse
import bisect
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

from state import fingerprint


_EXCLUDES = ("__pycache__", "*.pyc", "dist", ".git")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9+_.-]*\Z")
_CONFIG_REF = re.compile(r"\bBR2_[A-Za-z0-9_]+\b")
_CONFIG_ASSIGNMENT = re.compile(r"(BR2_[A-Za-z0-9_]+)=(.*)\Z")
_NOT_SET = re.compile(r"# (BR2_[A-Za-z0-9_]+) is not set\Z")
# These describe configuration provenance or download/cache policy, not an ABI.
_CONFIG_METADATA = {
    "BR2_DEFCONFIG", "BR2_DL_DIR", "BR2_JLEVEL", "BR2_PRIMARY_SITE",
    "BR2_PRIMARY_SITE_ONLY", "BR2_BACKUP_SITE", "BR2_KERNEL_MIRROR", "BR2_GNU_MIRROR",
    "BR2_LUAROCKS_MIRROR", "BR2_CPAN_MIRROR", "BR2_GOOGLE_MIRROR",
    "BR2_EXTERNAL_NAMES", "BR2_HAVE_DOT_CONFIG", "BR2_TARGET_ROOTFS_OCI_LABELS",
}
_DATA_FIELDS = ("KCONFIG_FILE", "KCONFIG_FRAGMENT_FILES", "CONFIG_FILE")
_MAKE_FUNCTIONS = set("subst patsubst strip findstring filter filter-out sort word wordlist words "
                      "firstword lastword dir notdir suffix basename addsuffix addprefix join "
                      "wildcard realpath abspath if or and intcmp foreach let call value eval "
                      "origin flavor shell file error warning info guile".split())
_EPHEMERAL_MAKE_VARS = frozenset({
    "MAKEFLAGS", "MFLAGS", "MAKELEVEL", "MAKE_TERMOUT", "MAKE_TERMERR",
    "DATE", "BR2_LOCALVERSION", "BR2_VERSION_FULL", "BR2_VERSION_GIT_EPOCH",
    "CUR_UMASK", "BR_ORIG_UMASK", "PUU_PACKAGE_STATE_DIR",
    "-*-command-variables-*-", "-*-eval-flags-*-",
})
_ACCUMULATOR_GLOBALS = frozenset({
    "KEEP_PYTHON_PY_FILES", "PACKAGES_BUSYBOX_CONFIG_FIXUPS",
    "PACKAGES_LINUX_CONFIG_FIXUPS", "PACKAGES_SELINUX_MODULES",
    "PACKAGES_SELINUX_EXTRA_MODULES_DIRS",
})


def _split_call_arguments(text):
    arguments, start, parens, braces = [], 0, 0, 0
    for index, char in enumerate(text):
        if char == "(":
            parens += 1
        elif char == ")":
            parens = max(0, parens - 1)
        elif char == "{":
            braces += 1
        elif char == "}":
            braces = max(0, braces - 1)
        elif char == "," and parens == 0 and braces == 0:
            arguments.append(text[start:index].strip())
            start = index + 1
    arguments.append(text[start:].strip())
    return arguments


def _bind_context(text, prefix, params=()):
    def replace(match):
        if match.group(0) == "$$":
            return "$$"
        token = match.group(1) or match.group(2)
        if token == "PKG":
            return prefix
        position = int(token)
        return params[position - 1] if position <= len(params) else ""

    return re.sub(r"\$\$|\$\((PKG|[1-9])\)|\$\{(PKG|[1-9])\}", replace, text)


def _scan_bound_make(text):
    """Discover dependencies without evaluating Make; unknown forms set opaque."""
    references, calls, prefixes, suffixes = set(), set(), set(), set()
    opaque = False
    index = 0
    while index < len(text):
        if text[index] != "$" or index + 1 == len(text):
            index += 1
            continue
        opening = text[index + 1]
        if opening == "$":
            index += 2
            continue
        if opening not in "({":
            if opening not in "@%<?^+*|0123456789":
                references.add(opening)
            index += 2
            continue
        closing = ")" if opening == "(" else "}"
        end, depth = index + 2, 1
        while end < len(text) and depth:
            depth += (text[end] == opening) - (text[end] == closing)
            end += 1
        if depth:
            return references, calls, prefixes, suffixes, True
        body = text[index + 2:end - 1].strip()
        if not body or re.fullmatch(r"(?:[@%<?^+*|][DF]?|[0-9]+|\$\(strip\s*\))", body):
            pass
        elif re.fullmatch(r"[A-Za-z_.][A-Za-z0-9_.-]*", body):
            references.add(body)
        elif match := re.fullmatch(r"\$\(strip\s+([A-Za-z_.][A-Za-z0-9_.-]*)\)", body):
            references.add(match[1])
        else:
            match = re.match(r"([A-Za-z-]+)(?:\s+|\Z)", body)
            if match and match[1] in _MAKE_FUNCTIONS:
                function = match[1]
                rest = body[match.end():]
                arguments = _split_call_arguments(rest) if rest else [""]
                if function == "call":
                    target = arguments[0]
                    if re.fullmatch(r"[A-Za-z_.][A-Za-z0-9_.-]*", target):
                        calls.add((target, tuple(arguments[1:])))
                    elif prefix_match := re.match(r"([A-Za-z_.][A-Za-z0-9_.-]*)\$", target):
                        prefixes.add(prefix_match[1])
                    else:
                        opaque = True
                elif function in ("value", "origin", "flavor"):
                    if re.fullmatch(r"[A-Za-z_.][A-Za-z0-9_.-]*", arguments[0]):
                        references.add(arguments[0])
                    else:
                        opaque = True
                elif function == "eval":
                    if not rest.lstrip().startswith("__tmp "):
                        opaque = True
                elif function == "guile":
                    opaque = True
            elif match := re.match(r"([A-Za-z_.][A-Za-z0-9_.-]*):", body):
                references.add(match[1])
            elif prefix_match := re.match(r"([A-Za-z_.][A-Za-z0-9_.-]*)\$", body):
                prefixes.add(prefix_match[1])
            elif suffix_match := re.fullmatch(
                    r"\$\(call\s+UPPERCASE,\$\([A-Za-z_][A-Za-z0-9_]*\)\)(_[A-Za-z0-9_]+)", body):
                suffixes.add(suffix_match[1])
            else:
                opaque = True
            nested_refs, nested_calls, nested_pfx, nested_sfx, unknown = _scan_bound_make(body)
            references.update(nested_refs)
            calls.update(nested_calls)
            prefixes.update(nested_pfx)
            suffixes.update(nested_sfx)
            opaque |= unknown
        index = end
    return references, calls, prefixes, suffixes, opaque


def _helper_inputs(data, variables, globals_data, universe=(), suffix_cache=None):
    if suffix_cache is None:
        suffix_cache = {}
    if not universe:
        universe = sorted(variables)
    prefix = data["prefix"]
    resolved = {**globals_data,
                **{f"{prefix}_{field}": value for field, value in data["expanded"].items()}}
    helpers = {}
    seen = set()
    opaque = False
    queue = [(name, (), 0, False) for name in data["definitions"]]
    for hooks in data["hooks"].values():
        queue.extend((hook, (), 0, True) for hook in hooks)

    def enqueue_matches(prefixes, suffixes):
        for pfx in prefixes:
            position = bisect.bisect_left(universe, pfx)
            while position < len(universe) and universe[position].startswith(pfx):
                queue.append((universe[position], (), 0, True))
                position += 1
        for sfx in suffixes:
            matches = suffix_cache.get(sfx)
            if matches is None:
                matches = [item for item in universe if item.endswith(sfx)]
                suffix_cache[sfx] = matches
            queue.extend((item, (), 0, True) for item in matches)

    for field, body in data["raw"].items():
        refs, calls, pfx, sfx, unknown = _scan_bound_make(_bind_context(body, prefix))
        queue.extend((name, (), 0, True) for name in refs)
        queue.extend((target, args, 1, True) for target, args in calls)
        enqueue_matches(pfx, sfx)
        opaque |= unknown
        if field.endswith("_HOOKS"):
            queue.extend((word, (), 0, True)
                         for word in re.findall(r"[A-Za-z_][A-Za-z0-9_.-]*", body)
                         if word in variables)
    while queue:
        name, params, depth, called = queue.pop()
        if name in _EPHEMERAL_MAKE_VARS or name.startswith("PUU_STATE_"):
            continue
        if (name, params, called) in seen:
            continue
        seen.add((name, params, called))
        record = ({"value": resolved[name], "flavor": "simple"} if name in resolved else
                  variables.get(name, {"value": "", "flavor": "undefined"}))
        helpers.setdefault(name, record)
        body = record["value"]
        if name.endswith("_HOOKS"):
            queue.extend((word, (), 0, True)
                         for word in re.findall(r"[A-Za-z_][A-Za-z0-9_.-]*", body)
                         if word in variables)
        if record["flavor"] != "recursive":
            continue
        if not called and re.search(r"\$(?:[1-9]|\([1-9]\)|\{[1-9]\})", body):
            continue
        if depth > 6:
            opaque = True
            continue
        refs, calls, pfx, sfx, unknown = _scan_bound_make(_bind_context(body, prefix, params))
        queue.extend((target, (), 0, True) for target in refs)
        queue.extend((target, args, depth + 1, True) for target, args in calls)
        enqueue_matches(pfx, sfx)
        opaque |= unknown
    return helpers, opaque


def _key(value):
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, ensure_ascii=True, separators=(",", ":")
    ).encode()).hexdigest()


def _read_config(path):
    config = {}
    for line in path.read_text().splitlines():
        if match := _CONFIG_ASSIGNMENT.fullmatch(line):
            name, value = match.groups()
        elif match := _NOT_SET.fullmatch(line):
            name, value = match.group(1), "n"
        elif not line or line.startswith("#"):
            continue
        else:
            raise ValueError(f"unrecognized configuration line in {path}: {line!r}")
        if name in config:
            raise ValueError(f"duplicate configuration symbol: {name}")
        config[name] = value
    if config.get("BR2_HAVE_DOT_CONFIG") != "y":
        raise ValueError(f"not a configured Buildroot output directory: {path.parent}")
    return config


def _global_config(config):
    # Unknown non-package symbols deliberately remain infrastructure inputs.
    # Linux is owned by its package; image/toolchain/global policy is conservative.
    return {
        name: value for name, value in config.items()
        if not name.startswith(("BR2_PACKAGE_", "BR2_LINUX_KERNEL", "BR2_CCACHE"))
        and not (name.startswith("BR2_EXTERNAL_") and name.endswith(("_PATH", "_VERSION")))
        and name not in _CONFIG_METADATA
    }


def _normalizer(root, board, source, download=None):
    items = [(root, "<external>"), (board, "<output>"), (source, "<buildroot>")]
    if download is not None:
        items.append((download, "<download>"))
    prefixes = sorted(
        ((str(path), label) for path, label in items),
        key=lambda item: len(item[0]), reverse=True,
    )
    pattern = re.compile(
        "(?:" + "|".join(re.escape(prefix) for prefix, _ in prefixes) + r")(?![A-Za-z0-9_.+-])"
    )
    labels = dict(prefixes)

    def normalize(value):
        if isinstance(value, str):
            return pattern.sub(lambda match: labels[match.group()], value)
        if isinstance(value, list):
            return [normalize(item) for item in value]
        if isinstance(value, dict):
            return {name: normalize(item) for name, item in value.items()}
        return value

    return normalize


def _content(path):
    """Use state.fingerprint with stable, relative labels rather than scratch paths."""
    previous = os.open(".", os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.chdir(path.parent)
        return fingerprint([path.name], excludes=_EXCLUDES)
    finally:
        os.fchdir(previous)
        os.close(previous)


def _input_path(value, root, board, source, *, board_relative=False):
    path = Path(value)
    if not path.is_absolute():
        base = board if board_relative and (board / path).exists() else source
        path = base / path
    path = Path(os.path.normpath(path))
    # Never fingerprint installed trees, stamp directories, or dependency caches.
    resolved = path.resolve()
    if resolved.is_relative_to(board) and not resolved.is_relative_to(source):
        raise ValueError(f"configured data input points into build output: {path}")
    if not (resolved.is_relative_to(root) or resolved.is_relative_to(source)):
        raise ValueError(f"unclassified package input outside external/Buildroot trees: {path}")
    if not path.exists():
        raise FileNotFoundError(f"missing configured package input: {path}")
    return path


def _build_dirs(info):
    # Only PKG_DIR has -dirclean semantics; source/build directories may be
    # shared, nested, or outside O (for example OVERRIDE_SRCDIR).
    value = info.get("stamp_dir")
    if not isinstance(value, str):
        raise ValueError(f"missing or invalid stamp_dir: {info!r}")
    if not value:
        if info["virtual"]:
            return []
        raise ValueError(f"missing nonvirtual stamp_dir: {info!r}")
    path = Path(value)
    if (path.is_absolute() or len(value.split("/")) != 2 or len(path.parts) != 2
            or path.parts[0] != "build" or path.parts[1] in (".", "..")):
        raise ValueError(f"unsafe stamp_dir: {value!r}")
    return [path.as_posix()]


def _metadata(info):
    # A newly enabled consumer must not change a dependency's own identity.
    return {name: value for name, value in info.items()
            if name not in ("dependencies", "reverse_dependencies")}


def _make_arguments(make_args, root, board, source):
    """Accept the wrapper's options/assignments, never additional make goals/code."""
    index = 0
    assignments = {}
    while index < len(make_args):
        arg = make_args[index]
        if arg in ("-j", "-l", "-C"):
            index += 1
            if index == len(make_args):
                raise ValueError(f"missing value after {arg}")
            value = make_args[index]
            if arg == "-C":
                if Path(value).resolve() != source:
                    raise ValueError("make -C does not match source")
            elif not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value):
                raise ValueError(f"invalid make option: {arg} {value}")
        elif re.fullmatch(r"-[jl][0-9]+(?:\.[0-9]+)?", arg):
            pass
        elif arg.startswith("-C") and Path(arg[2:]).resolve() == source:
            pass
        elif match := re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)=(.*)", arg, re.DOTALL):
            name, value = match.groups()
            assignments[name] = value
        else:
            raise ValueError(f"unsupported metadata make argument: {arg!r}")
        index += 1
    for name, expected in (("O", board), ("BR2_EXTERNAL", root)):
        if name in assignments and Path(assignments[name]).resolve() != expected:
            raise ValueError(f"make {name} does not match collector input")
    # Keep job/cache/location options out of infrastructure. All unclassified
    # command-line overrides fail closed by changing the infrastructure identity.
    overrides = {name: value for name, value in assignments.items()
                 if name not in {"O", "BR2_EXTERNAL", "BR2_CCACHE_DIR", "BR2_DL_DIR",
                                 "PUU_VERSION", "PUU_VARIANT"}}
    return [*make_args, "-C", str(source), f"O={board}", f"BR2_EXTERNAL={root}"], overrides


def collect(root: Path, board: Path, source: Path, make_args: list[str]) -> dict:
    """Return a version-1 snapshot of the configured package graph and identities.

    Raises on missing inputs, unsupported metadata, unsafe directories or failed
    make invocations. Callers own comparison, locking, journaling and deletion.
    """
    root, board, source = (path.resolve(strict=True) for path in (root, board, source))
    config = _read_config(board / ".config")
    if config.get("BR2_PER_PACKAGE_DIRECTORIES") != "y":
        raise ValueError("safe invalidation requires BR2_PER_PACKAGE_DIRECTORIES=y")
    arguments, overrides = _make_arguments(make_args, root, board, source)
    collector = Path(__file__).resolve().with_name("package-state.mk")
    with tempfile.TemporaryDirectory(prefix="puu-package-state-") as temporary:
        result = subprocess.run(
            ["make", *arguments, "--no-print-directory", "-s", "-f", str(source / "Makefile"),
             "-f", str(collector), f"PUU_PACKAGE_STATE_DIR={temporary}",
             "show-info"],
            cwd=source, text=True, stdout=subprocess.PIPE, check=True, umask=0o022,
        )
        upstream = json.loads(result.stdout)
        if not isinstance(upstream, dict) or not upstream:
            raise ValueError("Buildroot show-info did not return a package graph")
        globals_data = json.loads((Path(temporary) / "globals.json").read_text())
        if (not isinstance(globals_data, dict)
                or not all(isinstance(value, str) for value in globals_data.values())
                or not globals_data.get("BR2_VERSION")):
            raise ValueError("invalid Buildroot global metadata")
        download = Path(globals_data["DL_DIR"]).resolve() if globals_data.get("DL_DIR") else None
        normalize = _normalizer(root, board, source, download)
        raw_data = json.loads((Path(temporary) / "raw-definitions.json").read_text())
        variables = raw_data.get("variables")
        if (not isinstance(variables, dict)
                or not all(isinstance(record, dict) and isinstance(record.get("value"), str)
                           and record.get("flavor") in ("simple", "recursive", "undefined")
                           for record in variables.values())):
            raise ValueError("invalid raw Make helper metadata")
        records = {}
        for name, info in upstream.items():
            if not _NAME.fullmatch(name) or not isinstance(info, dict):
                raise ValueError(f"invalid upstream package metadata: {name!r}")
            if info.get("type") == "rootfs":
                continue
            if info.get("type") not in ("host", "target") or type(info.get("virtual")) is not bool:
                raise ValueError(f"unsupported package type/virtual metadata: {name}")
            data = json.loads((Path(temporary) / f"{name}.json").read_text())
            if not isinstance(data, dict) or data.get("info") != {name: info}:
                raise ValueError(f"inconsistent package metadata: {name}")
            for field in ("expanded", "raw", "hooks", "definitions"):
                if not isinstance(data.get(field), dict):
                    raise ValueError(f"missing bounded {field} metadata: {name}")
            if not all(isinstance(value, str) for field in ("expanded", "raw", "definitions")
                       for value in data[field].values()):
                raise ValueError(f"invalid bounded inputs: {name}")
            if (not isinstance(data.get("prefix"), str)
                    or not all(isinstance(hooks, dict)
                               and all(isinstance(body, str) for body in hooks.values())
                               for hooks in data["hooks"].values())):
                raise ValueError(f"invalid hook metadata: {name}")
            records[name] = data

    packages = {}
    identity_inputs = {}
    content_cache = {}
    universe = sorted(set(variables) | set(config))
    suffix_cache = {}

    def content(path):
        if path not in content_cache:
            content_cache[path] = _content(path)
        return [normalize(str(path)), content_cache[path]]

    for name, data in sorted(records.items()):
        info = data["info"][name]
        dependencies = info.get("dependencies")
        if (not isinstance(dependencies, list)
                or not all(isinstance(dep, str) and dep in records for dep in dependencies)):
            raise ValueError(f"incomplete package dependency graph: {name}")
        pkgdir = info.get("package_dir")
        if not isinstance(pkgdir, str) or not pkgdir:
            raise ValueError(f"missing package_dir: {name}")
        recipe = _input_path(pkgdir, root, board, source)
        if not recipe.is_dir():
            raise ValueError(f"package_dir is not a directory: {recipe}")
        inputs = [content(recipe)]
        rawname = info.get("name")
        if not isinstance(rawname, str) or not _NAME.fullmatch(rawname):
            raise ValueError(f"invalid upstream raw package name: {name}")
        patches = root / "patches" / rawname
        if patches.exists():
            inputs.append(content(patches))
        for field in ("hashes", "patches"):
            paths = info.get(field, [])
            if not isinstance(paths, list) or not all(isinstance(path, str) for path in paths):
                raise ValueError(f"invalid upstream {field}: {name}")
            for value in paths:
                if "://" in value:
                    continue
                # Upstream hash paths are source-relative, patches O-relative.
                inputs.append(content(_input_path(
                    value, root, board, source, board_relative=field == "patches"
                )))
        expanded = data["expanded"]
        for field in _DATA_FIELDS:
            for value in expanded.get(field, "").split():
                inputs.append(content(_input_path(value, root, board, source)))
        local_source = expanded.get("OVERRIDE_SRCDIR", "")
        if not local_source and expanded.get("SITE_METHOD") == "local":
            local_source = expanded.get("SITE", "")
        if local_source:
            inputs.append(content(_input_path(local_source, root, board, source)))
        helpers, opaque = _helper_inputs(data, variables, globals_data, universe, suffix_cache)
        references = set(_CONFIG_REF.findall(json.dumps(
            [data["definitions"], data["raw"], data["hooks"], helpers]
        )))
        if opaque:
            references.update(symbol for symbol in config if symbol.startswith("BR2_PACKAGE_"))
        package_config = {
            symbol: config.get(symbol, "n") for symbol in sorted(references)
            if not (symbol.startswith("BR2_EXTERNAL_")
                    and symbol.endswith(("_PATH", "_VERSION")))
        }
        if name == "linux":
            inputs.append(content(root / "linux"))
            package_config.update({symbol: value for symbol, value in config.items()
                                   if symbol.startswith("BR2_LINUX_KERNEL")})
        identity_inputs[name] = normalize({
            "metadata": _metadata(info), "expanded": expanded,
            "raw": data["raw"], "hooks": data["hooks"], "definitions": data["definitions"],
            "config": package_config, "files": inputs, "helpers": helpers,
        })
        packages[name] = {
            "dependencies": sorted(set(dependencies)),
            "build_dirs": _build_dirs(info), "virtual": info["virtual"],
            "conservative": opaque,
        }
    for name, package in packages.items():
        inputs = identity_inputs[name]
        if name == "linux":
            inputs["dependency_metadata"] = {
                dep: {key: value for key, value in identity_inputs[dep].items()
                      if key != "files"}
                for dep in package["dependencies"]
            }
        package["identity"] = _key(inputs)

    # Only common infrastructure, never all package recipes or external.mk.
    common = [source / name for name in (
        "Makefile", "Makefile.legacy", "Config.in", "Config.in.legacy", "arch",
        "toolchain", "support", "system", "package/Makefile.in",
    )]
    common.extend(sorted((source / "package").glob("pkg-*.mk")))
    common.extend(root / name for name in ("sdk", "Config.in", "external.desc"))
    infrastructure = normalize({
        "files": [content(path) for path in common],
        "globals": {name: value for name, value in globals_data.items()
                    if name not in _ACCUMULATOR_GLOBALS},
        "config": _global_config(config), "overrides": overrides,
        "source_hash": (source.parent.name
                        if re.fullmatch(r"[0-9a-f]{64}", source.parent.name) else None),
    })
    return {
        "version": 1, "infrastructure": _key(infrastructure),
        "external_mk": (root / "external.mk").read_text(), "packages": packages,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    snapshot = subparsers.add_parser("snapshot")
    for name in ("root", "board", "source", "output"):
        snapshot.add_argument(f"--{name}", type=Path, required=True)
    argv = sys.argv[1:]
    boundary = argv.index("--") if "--" in argv else len(argv)
    args = parser.parse_args(argv[:boundary])
    try:
        data = collect(args.root, args.board, args.source, argv[boundary + 1:])
        # A failed collection must never overwrite a previous successful snapshot.
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", dir=args.output.parent, delete=False
            ) as handle:
                temporary = Path(handle.name)
                json.dump(data, handle, sort_keys=True, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, args.output)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"package-state: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
