#!/usr/bin/env python3
"""Fetch Govee's official H60A6 scene library for 100% offline local playback.

Queries Govee's unauthenticated catalog endpoint once and saves a normalized
`scenes_h60a6.json` file inside `custom_components/govee_h60a6_local/`.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import sys
import types
from typing import Any
import urllib.request

COMPONENT_DIR = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "govee_h60a6_local"
)
DEFAULT_OUTPUT = COMPONENT_DIR / "scenes_h60a6.json"
GOVEE_LIBRARY_URL = "https://app2.govee.com/appsku/v1/light-effect-libraries"


def _load_scenes_module() -> Any:
    pkg_name = "_govee_fetch_scenes"
    if pkg_name not in sys.modules:
        pkg = types.ModuleType(pkg_name)
        pkg.__path__ = [str(COMPONENT_DIR)]  # type: ignore[attr-defined]
        sys.modules[pkg_name] = pkg

    for mod_short in ("const", "scenes"):
        full_name = f"{pkg_name}.{mod_short}"
        if full_name in sys.modules:
            continue
        spec = importlib.util.spec_from_file_location(
            full_name, COMPONENT_DIR / f"{mod_short}.py"
        )
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Could not load {mod_short}.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[full_name] = mod
        spec.loader.exec_module(mod)
    return sys.modules[f"{pkg_name}.scenes"]


scenes_mod = _load_scenes_module()


def fetch_raw_library(sku: str, timeout: float = 15.0) -> dict[str, Any]:
    """Download the Govee light-effect-libraries JSON payload for `sku`."""
    url = f"{GOVEE_LIBRARY_URL}?sku={sku}"
    req = urllib.request.Request(
        url,
        headers={
            "AppVersion": "5.6.01",
            "User-Agent": "GoveeHome/5.6.01",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fetch and bundle Govee H60A6 scenes for offline Home Assistant use"
    )
    parser.add_argument("--sku", default="H60A6", help="Govee SKU (default: H60A6)")
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Output JSON path (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--from-file",
        type=Path,
        default=None,
        help="Optional local raw light-effect-libraries JSON file instead of HTTP fetch",
    )
    args = parser.parse_args()

    if args.from_file is not None:
        raw = json.loads(args.from_file.read_text(encoding="utf-8"))
    else:
        print(f"Fetching official scene library for SKU {args.sku} from {GOVEE_LIBRARY_URL}...")
        raw = fetch_raw_library(args.sku)

    parsed = scenes_mod.parse_govee_library_json(raw)
    if not parsed:
        print("Warning: 0 scenes parsed from response.", file=sys.stderr)
        return 1

    payload = {"sku": args.sku, "scenes": parsed}
    args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"Saved {len(parsed)} scenes for {args.sku} to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
