#!/usr/bin/env python3
"""
Manage Suwayomi global library update interval.
Disables or adjusts the built-in periodic update interval via Suwayomi GraphQL API.
"""

import argparse
import json
import sys
import urllib.request
import urllib.error

GRAPHQL_URL = "http://127.0.0.1:4567/suwayomi/api/graphql"

def get_current_settings(url: str = GRAPHQL_URL) -> dict:
    query = """
    query GetSettings {
        settings {
            globalUpdateInterval
            updateMangas
        }
    }
    """
    req = urllib.request.Request(
        url,
        data=json.dumps({"query": query}).encode("utf-8"),
        headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        res = json.loads(resp.read().decode("utf-8"))
        if "errors" in res:
            raise RuntimeError(f"GraphQL error fetching settings: {res['errors']}")
        return res["data"]["settings"]

def set_global_update_interval(interval: float, url: str = GRAPHQL_URL) -> dict:
    mutation = """
    mutation SetGlobalUpdateInterval($input: SetSettingsInput!) {
        setSettings(input: $input) {
            settings {
                globalUpdateInterval
                updateMangas
            }
        }
    }
    """
    variables = {
        "input": {
            "settings": {
                "globalUpdateInterval": float(interval)
            }
        }
    }
    payload = json.dumps({"query": mutation, "variables": variables}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        res = json.loads(resp.read().decode("utf-8"))
        if "errors" in res:
            raise RuntimeError(f"GraphQL error setting globalUpdateInterval: {res['errors']}")
        return res["data"]["setSettings"]["settings"]

def main():
    parser = argparse.ArgumentParser(description="Configure Suwayomi globalUpdateInterval via GraphQL")
    parser.add_argument("--url", default=GRAPHQL_URL, help=f"GraphQL endpoint (default: {GRAPHQL_URL})")
    parser.add_argument("--interval", type=float, default=0.0, help="Interval in hours (0.0 to disable, default: 0.0)")
    parser.add_argument("--dry-run", action="store_true", help="Inspect current setting without modifying")
    args = parser.parse_args()

    try:
        current = get_current_settings(args.url)
        print(f"Current settings: globalUpdateInterval={current.get('globalUpdateInterval')}, updateMangas={current.get('updateMangas')}")

        if args.dry_run:
            print(f"Dry-run mode: would set globalUpdateInterval={args.interval}")
            return 0

        if current.get("globalUpdateInterval") == args.interval:
            print(f"globalUpdateInterval is already {args.interval}. No change needed.")
            return 0

        updated = set_global_update_interval(args.interval, args.url)
        print(f"Updated settings: globalUpdateInterval={updated.get('globalUpdateInterval')}, updateMangas={updated.get('updateMangas')}")
        return 0
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

if __name__ == "__main__":
    sys.exit(main())
