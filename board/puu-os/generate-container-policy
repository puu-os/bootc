#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) Opinsys Oy 2026

import json
import sys


def generate_policy(repo: str, keys: list[str]) -> dict:
    namespace = repo.rpartition("/")[0] if "/" in repo else repo
    if keys:
        signed = [{
            "type": "sigstoreSigned",
            "keyPaths": keys,
            "signedIdentity": {"type": "exactRepository", "dockerRepository": repo},
        }]
        namespace_signed = [{
            "type": "sigstoreSigned",
            "keyPaths": keys,
            "signedIdentity": {"type": "matchRepository"},
        }]
        transports = {
            "oci-archive": {
                "/run/puu-installer/payload/image.tar": [
                    {"type": "insecureAcceptAnything"},
                ],
            },
            "oci": {"": signed},
            "dir": {"": signed},
            "docker": {namespace: namespace_signed},
        }
    else:
        accept = [{"type": "insecureAcceptAnything"}]
        transports = {
            "oci-archive": {"": accept},
            "oci": {"": accept},
            "docker": {namespace: accept},
        }

    return {"default": [{"type": "reject"}], "transports": transports}


def main() -> None:
    if len(sys.argv) < 2:
        print(f"usage: {sys.argv[0]} <repo> [key_path ...]", file=sys.stderr)
        sys.exit(1)
    policy = generate_policy(sys.argv[1], sys.argv[2:])
    json.dump(policy, sys.stdout, indent=4)
    print()


if __name__ == "__main__":
    main()
