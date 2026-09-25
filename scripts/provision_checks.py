#!/usr/bin/env python
"""Bootstrap deadman checks from a YAML manifest (checks-as-code).

One command provisions every monitoring check this stack needs after a fresh
deploy, using the fork's slug auto-provisioning (ping a slug URL once and the
check exists — no API-token dance).

Usage:
    # From the host (8100 published) or inside the compose network:
    python scripts/provision_checks.py --site-root http://localhost:8100 \
        --manifest scripts/checks.yml

    # Print URLs without pinging (dry run):
    python scripts/provision_checks.py --dry-run

The manifest lists checks with slug (used in the ping URL), name, period
(seconds between expected pings), grace (seconds before 'down'), and tags.
The first successful ping creates the check; the period/grace/tags params
tune it at creation. Safe to re-run: pinging an existing check never
re-creates it (idempotent by design).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from urllib.parse import quote

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore[assignment]

DEFAULT_MANIFEST = Path(__file__).with_name("checks.yml")


def load_manifest(path: Path) -> list[dict]:
    if yaml is None:
        raise SystemExit("PyYAML required: pip install pyyaml (or use --json manifest)")
    with path.open("r", encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
    checks = doc.get("checks") if isinstance(doc, dict) else doc
    if not isinstance(checks, list):
        raise SystemExit(f"{path}: expected a list of checks or {{checks: [...]}}")
    return [c for c in checks if isinstance(c, dict) and c.get("slug")]


def ping_url(site_root: str, ping_key: str, check: dict) -> str:
    """Build the ping URL for one check (params only tune CREATION).

    period/grace/tags/name are consumed by the fork's auto-provisioner on the
    first ping; after that they are ignored by /ping (harmless to keep).
    """
    from urllib.parse import urlencode

    slug = quote(str(check["slug"]), safe="")
    params: dict[str, str] = {}
    if check.get("name"):
        params["name"] = str(check["name"])
    if check.get("period"):
        params["period"] = str(int(check["period"]))
    if check.get("grace"):
        params["grace"] = str(int(check["grace"]))
    if check.get("tags"):
        params["tags"] = " ".join(str(t) for t in check["tags"])
    qs = f"?{urlencode(params)}" if params else ""
    return f"{site_root.rstrip('/')}/ping/{ping_key}/{slug}{qs}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site-root", default="http://localhost:8100")
    parser.add_argument("--ping-key", required=True,
                        help="Project ping key (healthchecks UI → Project Settings → Ping Key)")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--dry-run", action="store_true",
                        help="Print ping URLs; do not ping")
    args = parser.parse_args()

    checks = load_manifest(args.manifest)
    print(f"manifest: {len(checks)} check(s) from {args.manifest}")

    if args.dry_run:
        for c in checks:
            print(f"  {ping_url(args.site_root, args.ping_key, c)}")
        return 0

    import urllib.request

    failures = 0
    for c in checks:
        url = ping_url(args.site_root, args.ping_key, c)
        try:
            req = urllib.request.Request(url, data=b"", method="GET")
            with urllib.request.urlopen(req, timeout=10) as resp:
                status = resp.status
        except Exception as exc:  # noqa: BLE001
            status = f"error: {exc}"
            failures += 1
        print(f"  [{status}] {c['slug']} -> {url.split('?')[0]}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())