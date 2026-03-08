#!/usr/bin/env python3
"""Memory snapshot & restore for test runs.

Captures and restores the memory DB, FAISS index, and chat history
so test runs don't contaminate production memory.

Usage:
    python3 scripts/memory_snapshot.py snapshot          # Save current state
    python3 scripts/memory_snapshot.py restore           # Restore saved state
    python3 scripts/memory_snapshot.py snapshot --tag v1  # Named snapshot
    python3 scripts/memory_snapshot.py restore --tag v1   # Restore named snapshot

Intended workflow:
    1. snapshot before test run
    2. run tests
    3. restore after test run
"""

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

# Paths
DATA_DIR = Path("/mnt/storage/jarvis/data")
MEMORY_DB = DATA_DIR / "memory.db"
FAISS_DIR = DATA_DIR / "memory_faiss"
CHAT_HISTORY = DATA_DIR / "conversations" / "chat_history.jsonl"
SNAPSHOT_DIR = DATA_DIR / "snapshots"


def snapshot(tag: str = "latest"):
    """Save current memory state."""
    snap_dir = SNAPSHOT_DIR / tag
    snap_dir.mkdir(parents=True, exist_ok=True)

    copied = []

    # SQLite DB
    if MEMORY_DB.exists():
        shutil.copy2(MEMORY_DB, snap_dir / "memory.db")
        copied.append("memory.db")

    # FAISS index + metadata
    faiss_snap = snap_dir / "memory_faiss"
    if FAISS_DIR.exists():
        if faiss_snap.exists():
            shutil.rmtree(faiss_snap)
        shutil.copytree(FAISS_DIR, faiss_snap)
        copied.append("memory_faiss/")

    # Chat history
    if CHAT_HISTORY.exists():
        shutil.copy2(CHAT_HISTORY, snap_dir / "chat_history.jsonl")
        copied.append("chat_history.jsonl")

    # Metadata
    meta = {
        "tag": tag,
        "timestamp": time.time(),
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "files": copied,
    }
    with open(snap_dir / "snapshot_meta.json", "w") as f:
        json.dump(meta, f, indent=2)

    print(f"Snapshot '{tag}' saved to {snap_dir}")
    print(f"  Files: {', '.join(copied)}")
    return snap_dir


def restore(tag: str = "latest"):
    """Restore memory state from snapshot."""
    snap_dir = SNAPSHOT_DIR / tag
    if not snap_dir.exists():
        print(f"Error: snapshot '{tag}' not found at {snap_dir}")
        sys.exit(1)

    restored = []

    # SQLite DB
    db_snap = snap_dir / "memory.db"
    if db_snap.exists():
        shutil.copy2(db_snap, MEMORY_DB)
        restored.append("memory.db")

    # FAISS index
    faiss_snap = snap_dir / "memory_faiss"
    if faiss_snap.exists():
        if FAISS_DIR.exists():
            shutil.rmtree(FAISS_DIR)
        shutil.copytree(faiss_snap, FAISS_DIR)
        restored.append("memory_faiss/")
    else:
        # No FAISS in snapshot — clear current (will rebuild on restart)
        for f in FAISS_DIR.glob("*"):
            f.unlink()
        restored.append("memory_faiss/ (cleared)")

    # Chat history
    hist_snap = snap_dir / "chat_history.jsonl"
    if hist_snap.exists():
        shutil.copy2(hist_snap, CHAT_HISTORY)
        restored.append("chat_history.jsonl")

    print(f"Restored snapshot '{tag}' from {snap_dir}")
    print(f"  Files: {', '.join(restored)}")
    print("  Note: restart jarvis-web.service to pick up changes")


def main():
    parser = argparse.ArgumentParser(description="Memory snapshot & restore")
    parser.add_argument("action", choices=["snapshot", "restore"],
                        help="snapshot or restore")
    parser.add_argument("--tag", default="latest",
                        help="Snapshot name (default: latest)")
    args = parser.parse_args()

    if args.action == "snapshot":
        snapshot(args.tag)
    else:
        restore(args.tag)


if __name__ == "__main__":
    main()
