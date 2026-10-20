#!/usr/bin/env python3
"""Compare a freshly built site state with the state of the published site.

The preview workflow uses this to skip a deployment when nothing changed.

    python3 tools/compare_state.py --new _site/state.json --published-url URL

Prints ``changed=true`` or ``changed=false`` and writes the same value to the
file named by ``$GITHUB_OUTPUT`` when that variable is set.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import urllib.error
import urllib.request


def fingerprint(state: dict) -> dict:
    """The parts of the state that decide whether a redeploy is needed."""
    return {
        name: (entry.get("ref"), entry.get("sha"), entry.get("status"))
        for name, entry in state.get("projects", {}).items()
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--new", required=True, help="state.json of the new build")
    parser.add_argument(
        "--published-url",
        default="",
        help="base URL of the published site (its state.json is fetched)",
    )
    args = parser.parse_args()

    new = json.loads(pathlib.Path(args.new).read_text(encoding="utf-8"))
    old: dict = {}

    url = args.published_url.rstrip("/")
    if url.startswith("http"):
        url += "/state.json"
        try:
            with urllib.request.urlopen(url, timeout=30) as response:
                old = json.loads(response.read())
        except (urllib.error.URLError, urllib.error.HTTPError, OSError) as error:
            print(f"no published state to compare with: {error}")

    changed = fingerprint(new) != fingerprint(old)
    print(f"published={args.published_url}")
    print(f"changed={'true' if changed else 'false'}")

    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            handle.write(f"changed={'true' if changed else 'false'}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
