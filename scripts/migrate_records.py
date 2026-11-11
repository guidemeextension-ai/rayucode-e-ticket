"""Validate a local JSON export; only --apply writes to an empty Redis store."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import record_store


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="JSON records export (never modified)")
    parser.add_argument("--apply", action="store_true", help="import into empty Upstash storage")
    args = parser.parse_args(argv)
    try:
        records = json.loads(args.source.read_text(encoding="utf-8"))
        record_store.validate_records(records)
        if args.apply:
            record_store.import_records(records)
            print(f"Imported {len(records)} records into empty durable storage.")
        else:
            print(f"Dry run: {len(records)} valid records; no storage requests or writes. Use --apply to import.")
        return 0
    except (OSError, ValueError):
        print("Migration failed: source must be a readable JSON records file.", file=sys.stderr)
    except record_store.StorageError as exc:
        print(f"Migration failed: {exc}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
