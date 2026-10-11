"""Create (or with --reset, recreate) the SQLite database from data/seed.json."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Settings, make_clock  # noqa: E402
from app.db import seed_database  # noqa: E402
from app.repository import Repository  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reset", action="store_true", help="delete the existing database first")
    args = parser.parse_args()
    settings = Settings()
    seed_database(settings.db_path, settings.seed_path, reset=args.reset)
    repo = Repository(settings.db_path, make_clock("2026-10-11T09:00:00+05:30"))
    print(f"Database: {settings.db_path}")
    for p in repo.services():
        print(f"  {p['id']}  {p['name']:<22} {p['service']:<15} {p['practitioner']}")
    print(f"  {len(repo.available_slots())} available slots, {len(repo.list_bookings())} bookings")


if __name__ == "__main__":
    main()
