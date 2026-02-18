#!/usr/bin/env python3
"""Seed the JARVIS user profiles database.

Creates the two admin profiles (the primary and secondary users) with their
preferred honorifics.  Safe to re-run — skips profiles that already exist.

Usage:
    python3 scripts/init_profiles.py          # Create default profiles
    python3 scripts/init_profiles.py --list    # List all profiles
    python3 scripts/init_profiles.py --reset   # Delete and recreate
"""

import sys
from pathlib import Path

# Ensure project root is on sys.path so core imports work
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.config import Config
from core.user_profile import get_profile_manager


# Default profiles to seed
DEFAULT_PROFILES = [
    {
        "user_id": "user",
        "name": "User",
        "honorific": "sir",
        "role": "admin",
    },
    {
        "user_id": "secondary_user",
        "name": "Guest",
        "honorific": "ma'am",
        "role": "admin",
    },
]


def seed_profiles(reset: bool = False):
    """Create default profiles in the database."""
    config = Config()
    pm = get_profile_manager(config)

    if reset:
        for p in DEFAULT_PROFILES:
            existing = pm.get_profile(p["user_id"])
            if existing:
                pm.delete_profile(p["user_id"])
                print(f"  Deleted existing profile: {p['user_id']}")

    created = 0
    skipped = 0
    for p in DEFAULT_PROFILES:
        existing = pm.get_profile(p["user_id"])
        if existing:
            print(f"  Skipped (already exists): {p['user_id']} "
                  f"({existing['name']}, {existing['honorific']})")
            skipped += 1
        else:
            pm.create_profile(
                user_id=p["user_id"],
                name=p["name"],
                honorific=p["honorific"],
                role=p["role"],
            )
            print(f"  Created: {p['user_id']} ({p['name']}, {p['honorific']})")
            created += 1

    print(f"\nDone — {created} created, {skipped} skipped.")


def list_profiles():
    """Print all profiles in the database."""
    config = Config()
    pm = get_profile_manager(config)
    profiles = pm.get_all()

    if not profiles:
        print("  (no profiles)")
        return

    for p in profiles:
        emb = "enrolled" if p.get("embedding_path") else "no embedding"
        print(f"  {p['id']:15s} | {p['name']:15s} | {p['honorific']:6s} | "
              f"{p['role']:6s} | {emb} | created {p['created_at']}")


def main():
    print("JARVIS Profile Initialization")
    print("=" * 40)

    if "--list" in sys.argv:
        print("\nAll profiles:")
        list_profiles()
    elif "--reset" in sys.argv:
        print("\nResetting profiles...")
        seed_profiles(reset=True)
        print("\nCurrent profiles:")
        list_profiles()
    else:
        print("\nSeeding profiles...")
        seed_profiles()
        print("\nCurrent profiles:")
        list_profiles()


if __name__ == "__main__":
    main()
