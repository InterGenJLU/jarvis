#!/usr/bin/env python3
"""
Test script for the JARVIS Desktop Manager.

Exercises all DesktopManager methods — D-Bus extension, wmctrl fallback,
volume, notifications, and clipboard.

Usage:
    python3 scripts/test_desktop_manager.py
    python3 scripts/test_desktop_manager.py --skip-clipboard   # if wl-clipboard not installed
"""

import argparse
import sys
import time
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.config import load_config
from core.desktop_manager import get_desktop_manager


def header(title):
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}")


def test_result(name, success, detail=""):
    status = "PASS" if success else "FAIL"
    marker = "+" if success else "-"
    print(f"  [{marker}] {name}: {status}" + (f" — {detail}" if detail else ""))
    return success


def main():
    parser = argparse.ArgumentParser(description="Test JARVIS Desktop Manager")
    parser.add_argument("--skip-clipboard", action="store_true",
                        help="Skip clipboard tests (wl-clipboard not installed)")
    args = parser.parse_args()

    config = load_config()
    dm = get_desktop_manager(config)

    passed = 0
    failed = 0

    # ── Extension health ──────────────────────────────────────────
    header("Extension Health")
    ext_ok = dm.available
    if test_result("Extension D-Bus", ext_ok, "connected" if ext_ok else "not available (using wmctrl fallback)"):
        passed += 1
    else:
        failed += 1

    health = dm.get_health()
    test_result("Health dict", isinstance(health, dict), str(health))
    passed += 1

    # ── List windows ──────────────────────────────────────────────
    header("Window Operations")
    windows = dm.list_windows()
    win_ok = isinstance(windows, list) and len(windows) > 0
    if test_result("List windows", win_ok, f"{len(windows)} windows found"):
        passed += 1
        # Print first 3 windows
        for w in windows[:3]:
            print(f"       - [{w.get('id', '?')}] {w.get('wm_class', '?')}: {w.get('title', '?')[:50]}")
    else:
        failed += 1

    # ── Active window ─────────────────────────────────────────────
    active = dm.get_active_window()
    active_ok = active is not None
    if test_result("Get active window", active_ok,
                   f"{active.get('title', '?')[:40]}" if active else "none"):
        passed += 1
    else:
        failed += 1

    # ── Find window ───────────────────────────────────────────────
    # Try to find a common window
    for search_term in ["terminal", "code", "brave", "chrome", "firefox"]:
        found = dm.find_window(app_name=search_term)
        if found:
            test_result("Find window", True, f"'{search_term}' -> {found.get('title', '?')[:40]}")
            passed += 1
            break
    else:
        test_result("Find window", False, "no common windows found to test")
        failed += 1

    # ── Workspaces ────────────────────────────────────────────────
    header("Workspace Operations")
    workspaces = dm.list_workspaces()
    ws_ok = isinstance(workspaces, list) and len(workspaces) > 0
    if test_result("List workspaces", ws_ok, f"{len(workspaces)} workspaces"):
        passed += 1
        for ws in workspaces:
            marker = " (active)" if ws.get("active") else ""
            print(f"       - Workspace {ws.get('index', '?')}: {ws.get('n_windows', 0)} windows{marker}")
    else:
        if not ext_ok:
            print("       (workspace ops require the extension)")
        failed += 1

    # ── Volume ────────────────────────────────────────────────────
    header("Volume Control")
    vol = dm.get_volume()
    vol_ok = vol is not None
    if test_result("Get volume", vol_ok, f"{vol}%" if vol is not None else "failed"):
        passed += 1
    else:
        failed += 1

    muted = dm.is_muted()
    mute_ok = muted is not None
    if test_result("Check mute", mute_ok, f"muted={muted}" if muted is not None else "failed"):
        passed += 1
    else:
        failed += 1

    # ── Notifications ─────────────────────────────────────────────
    header("Notifications")
    notif_ok = dm.send_notification("JARVIS Test", "Desktop manager test notification", "low")
    if test_result("Send notification", notif_ok):
        passed += 1
    else:
        failed += 1

    # ── Clipboard ─────────────────────────────────────────────────
    if not args.skip_clipboard:
        header("Clipboard")
        clip_set = dm.set_clipboard("JARVIS test clipboard")
        if test_result("Set clipboard", clip_set):
            passed += 1
            time.sleep(0.2)
            clip_get = dm.get_clipboard()
            if test_result("Get clipboard", clip_get == "JARVIS test clipboard",
                           f"got: {clip_get!r}"):
                passed += 1
            else:
                failed += 1
        else:
            failed += 1
            test_result("Get clipboard", False, "skipped (set failed)")
            failed += 1
    else:
        print("\n  (clipboard tests skipped — use --skip-clipboard)")

    # ── Summary ───────────────────────────────────────────────────
    header("Summary")
    total = passed + failed
    print(f"  {passed}/{total} tests passed")
    if failed:
        print(f"  {failed} FAILED")
    if not ext_ok:
        print(f"\n  NOTE: Extension not available — window tests used wmctrl fallback.")
        print(f"  For full Wayland support, install & enable the extension:")
        print(f"    ./scripts/install_desktop_extension.sh")
        print(f"    (then logout/login)")
    print()
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
