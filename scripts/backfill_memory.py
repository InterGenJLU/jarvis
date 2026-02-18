#!/usr/bin/env python3
"""
One-time script to backfill existing chat history into FAISS index.

Loads config, initializes MemoryManager with the sentence-transformer
embedding model, and indexes all existing messages from chat_history.jsonl.

Idempotent: if an index already exists with the expected count, it skips.

Usage:
    python3 scripts/backfill_memory.py [--force]
"""

import argparse
import os
import sys
import time

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config import load_config
from core.conversation import ConversationManager


def main():
    parser = argparse.ArgumentParser(description="Backfill FAISS index from chat history")
    parser.add_argument("--force", action="store_true",
                        help="Force re-index even if index already exists")
    args = parser.parse_args()

    print("Loading config...")
    config = load_config()

    print("Initializing conversation manager...")
    conversation = ConversationManager(config)

    # Count total messages to give an estimate
    all_messages = conversation.load_full_history()
    eligible = [m for m in all_messages if len(m.get("content", "").strip()) >= 10]
    print(f"Found {len(all_messages)} total messages, {len(eligible)} eligible for indexing")

    print("Loading sentence-transformer model (all-MiniLM-L6-v2)...")
    from sentence_transformers import SentenceTransformer
    embedding_model = SentenceTransformer("all-MiniLM-L6-v2")

    print("Initializing MemoryManager...")
    from core.memory_manager import get_memory_manager
    # Reset singleton in case it was already loaded
    import core.memory_manager as mm_module
    mm_module._instance = None

    mm = get_memory_manager(
        config=config,
        conversation=conversation,
        embedding_model=embedding_model,
    )

    # Check if already backfilled
    if mm.faiss_index is not None and mm.faiss_index.ntotal > 0 and not args.force:
        print(f"\nFAISS index already has {mm.faiss_index.ntotal} vectors "
              f"({len(mm.faiss_metadata)} metadata entries).")
        print(f"Eligible messages: {len(eligible)}")
        if mm.faiss_index.ntotal >= len(eligible) * 0.9:
            print("Index appears up-to-date. Use --force to re-index.")
            return
        else:
            print("Index is smaller than expected — re-indexing...")

    # Clear existing index for clean backfill
    if args.force and mm.faiss_index is not None and mm.faiss_index.ntotal > 0:
        print(f"Force mode: clearing existing index ({mm.faiss_index.ntotal} vectors)...")
        import faiss
        mm.faiss_index = faiss.IndexFlatIP(384)
        mm.faiss_metadata = []

    print(f"\nStarting backfill...")
    start = time.time()
    count = mm.backfill_history()
    elapsed = time.time() - start

    print(f"\nDone! Indexed {count} messages in {elapsed:.1f}s")
    if mm.faiss_index:
        print(f"FAISS index total: {mm.faiss_index.ntotal} vectors")
    print(f"Index saved to: {mm.faiss_index_path}")


if __name__ == "__main__":
    main()
