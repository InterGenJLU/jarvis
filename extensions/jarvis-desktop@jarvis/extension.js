/*
 * JARVIS Desktop Bridge — GNOME Shell Extension
 *
 * Exposes a D-Bus service at org.jarvis.Desktop that provides full
 * Wayland-native window management, workspace control, and system
 * operations for the JARVIS voice assistant.
 *
 * GNOME 46+ ES module syntax.
 */

import Gio from "gi://Gio";
import GLib from "gi://GLib";
import Meta from "gi://Meta";
import Shell from "gi://Shell";

import { Extension } from "resource:///org/gnome/shell/extensions/extension.js";
import * as Main from "resource:///org/gnome/shell/ui/main.js";

const DBUS_IFACE = `
<node>
  <interface name="org.jarvis.Desktop">
    <method name="Ping">
      <arg type="s" direction="out" name="version"/>
    </method>
    <method name="ListWindows">
      <arg type="s" direction="out" name="windows_json"/>
    </method>
    <method name="GetActiveWindow">
      <arg type="s" direction="out" name="window_json"/>
    </method>
    <method name="FocusWindow">
      <arg type="u" direction="in" name="window_id"/>
      <arg type="b" direction="out" name="success"/>
    </method>
    <method name="CloseWindow">
      <arg type="u" direction="in" name="window_id"/>
      <arg type="b" direction="out" name="success"/>
    </method>
    <method name="MinimizeWindow">
      <arg type="u" direction="in" name="window_id"/>
      <arg type="b" direction="out" name="success"/>
    </method>
    <method name="MaximizeWindow">
      <arg type="u" direction="in" name="window_id"/>
      <arg type="b" direction="out" name="success"/>
    </method>
    <method name="UnmaximizeWindow">
      <arg type="u" direction="in" name="window_id"/>
      <arg type="b" direction="out" name="success"/>
    </method>
    <method name="FullscreenWindow">
      <arg type="u" direction="in" name="window_id"/>
      <arg type="b" direction="out" name="success"/>
    </method>
    <method name="UnfullscreenWindow">
      <arg type="u" direction="in" name="window_id"/>
      <arg type="b" direction="out" name="success"/>
    </method>
    <method name="MoveResizeWindow">
      <arg type="u" direction="in" name="window_id"/>
      <arg type="i" direction="in" name="x"/>
      <arg type="i" direction="in" name="y"/>
      <arg type="i" direction="in" name="w"/>
      <arg type="i" direction="in" name="h"/>
      <arg type="b" direction="out" name="success"/>
    </method>
    <method name="ListWorkspaces">
      <arg type="s" direction="out" name="workspaces_json"/>
    </method>
    <method name="SwitchWorkspace">
      <arg type="u" direction="in" name="index"/>
      <arg type="b" direction="out" name="success"/>
    </method>
    <method name="MoveWindowToWorkspace">
      <arg type="u" direction="in" name="window_id"/>
      <arg type="u" direction="in" name="workspace_index"/>
      <arg type="b" direction="out" name="success"/>
    </method>
  </interface>
</node>`;

const VERSION = "1.0.0";

// ── Helpers ─────────────────────────────────────────────────────────

function _findWindow(stableId) {
  for (const actor of global.get_window_actors()) {
    const win = actor.get_meta_window();
    if (win && win.get_stable_sequence() === stableId) {
      return win;
    }
  }
  return null;
}

function _windowToJson(win) {
  const rect = win.get_frame_rect();
  const workspace = win.get_workspace();
  return {
    id: win.get_stable_sequence(),
    title: win.get_title() || "",
    wm_class: win.get_wm_class() || "",
    pid: win.get_pid(),
    workspace: workspace ? workspace.index() : -1,
    monitor: win.get_monitor(),
    minimized: win.minimized,
    maximized: win.get_maximized() === Meta.MaximizeFlags.BOTH,
    fullscreen: win.is_fullscreen(),
    x: rect.x,
    y: rect.y,
    width: rect.width,
    height: rect.height,
  };
}

// ── D-Bus Method Handlers ───────────────────────────────────────────

function _handlePing() {
  return new GLib.Variant("(s)", [VERSION]);
}

function _handleListWindows() {
  const windows = [];
  for (const actor of global.get_window_actors()) {
    const win = actor.get_meta_window();
    if (win && win.get_window_type() === Meta.WindowType.NORMAL) {
      windows.push(_windowToJson(win));
    }
  }
  return new GLib.Variant("(s)", [JSON.stringify(windows)]);
}

function _handleGetActiveWindow() {
  const win = global.display.get_focus_window();
  if (!win) {
    return new GLib.Variant("(s)", ["{}"]);
  }
  return new GLib.Variant("(s)", [JSON.stringify(_windowToJson(win))]);
}

function _handleFocusWindow(windowId) {
  const win = _findWindow(windowId);
  if (!win) return new GLib.Variant("(b)", [false]);
  const time = global.get_current_time();
  win.activate(time);
  return new GLib.Variant("(b)", [true]);
}

function _handleCloseWindow(windowId) {
  const win = _findWindow(windowId);
  if (!win) return new GLib.Variant("(b)", [false]);
  const time = global.get_current_time();
  win.delete(time);
  return new GLib.Variant("(b)", [true]);
}

function _handleMinimizeWindow(windowId) {
  const win = _findWindow(windowId);
  if (!win) return new GLib.Variant("(b)", [false]);
  win.minimize();
  return new GLib.Variant("(b)", [true]);
}

function _handleMaximizeWindow(windowId) {
  const win = _findWindow(windowId);
  if (!win) return new GLib.Variant("(b)", [false]);
  // Remove fullscreen first (conflicts with maximize on some setups)
  if (win.is_fullscreen()) win.unmake_fullscreen();
  win.maximize(Meta.MaximizeFlags.BOTH);
  return new GLib.Variant("(b)", [true]);
}

function _handleUnmaximizeWindow(windowId) {
  const win = _findWindow(windowId);
  if (!win) return new GLib.Variant("(b)", [false]);
  win.unmaximize(Meta.MaximizeFlags.BOTH);
  return new GLib.Variant("(b)", [true]);
}

function _handleFullscreenWindow(windowId) {
  const win = _findWindow(windowId);
  if (!win) return new GLib.Variant("(b)", [false]);
  // Remove maximize first
  if (win.get_maximized() === Meta.MaximizeFlags.BOTH) {
    win.unmaximize(Meta.MaximizeFlags.BOTH);
  }
  win.make_fullscreen();
  return new GLib.Variant("(b)", [true]);
}

function _handleUnfullscreenWindow(windowId) {
  const win = _findWindow(windowId);
  if (!win) return new GLib.Variant("(b)", [false]);
  win.unmake_fullscreen();
  return new GLib.Variant("(b)", [true]);
}

function _handleMoveResizeWindow(windowId, x, y, w, h) {
  const win = _findWindow(windowId);
  if (!win) return new GLib.Variant("(b)", [false]);
  // Unmaximize/unfullscreen before move-resize
  if (win.get_maximized() !== 0) win.unmaximize(Meta.MaximizeFlags.BOTH);
  if (win.is_fullscreen()) win.unmake_fullscreen();
  win.move_resize_frame(false, x, y, w, h);
  return new GLib.Variant("(b)", [true]);
}

function _handleListWorkspaces() {
  const manager = global.workspace_manager;
  const count = manager.get_n_workspaces();
  const active = manager.get_active_workspace_index();
  const workspaces = [];
  for (let i = 0; i < count; i++) {
    workspaces.push({
      index: i,
      active: i === active,
      n_windows: manager.get_workspace_by_index(i).n_windows,
    });
  }
  return new GLib.Variant("(s)", [JSON.stringify(workspaces)]);
}

function _handleSwitchWorkspace(index) {
  const manager = global.workspace_manager;
  if (index >= manager.get_n_workspaces()) {
    return new GLib.Variant("(b)", [false]);
  }
  const ws = manager.get_workspace_by_index(index);
  const time = global.get_current_time();
  ws.activate(time);
  return new GLib.Variant("(b)", [true]);
}

function _handleMoveWindowToWorkspace(windowId, wsIndex) {
  const win = _findWindow(windowId);
  if (!win) return new GLib.Variant("(b)", [false]);
  const manager = global.workspace_manager;
  if (wsIndex >= manager.get_n_workspaces()) {
    return new GLib.Variant("(b)", [false]);
  }
  win.change_workspace_by_index(wsIndex, false);
  return new GLib.Variant("(b)", [true]);
}

// ── D-Bus dispatch ──────────────────────────────────────────────────

function _onMethodCall(connection, sender, path, ifaceName, methodName, params, invocation) {
  try {
    let result;
    switch (methodName) {
      case "Ping":
        result = _handlePing();
        break;
      case "ListWindows":
        result = _handleListWindows();
        break;
      case "GetActiveWindow":
        result = _handleGetActiveWindow();
        break;
      case "FocusWindow":
        result = _handleFocusWindow(params.get_child_value(0).get_uint32());
        break;
      case "CloseWindow":
        result = _handleCloseWindow(params.get_child_value(0).get_uint32());
        break;
      case "MinimizeWindow":
        result = _handleMinimizeWindow(params.get_child_value(0).get_uint32());
        break;
      case "MaximizeWindow":
        result = _handleMaximizeWindow(params.get_child_value(0).get_uint32());
        break;
      case "UnmaximizeWindow":
        result = _handleUnmaximizeWindow(params.get_child_value(0).get_uint32());
        break;
      case "FullscreenWindow":
        result = _handleFullscreenWindow(params.get_child_value(0).get_uint32());
        break;
      case "UnfullscreenWindow":
        result = _handleUnfullscreenWindow(params.get_child_value(0).get_uint32());
        break;
      case "MoveResizeWindow": {
        const wid = params.get_child_value(0).get_uint32();
        const x = params.get_child_value(1).get_int32();
        const y = params.get_child_value(2).get_int32();
        const w = params.get_child_value(3).get_int32();
        const h = params.get_child_value(4).get_int32();
        result = _handleMoveResizeWindow(wid, x, y, w, h);
        break;
      }
      case "ListWorkspaces":
        result = _handleListWorkspaces();
        break;
      case "SwitchWorkspace":
        result = _handleSwitchWorkspace(params.get_child_value(0).get_uint32());
        break;
      case "MoveWindowToWorkspace": {
        const wid = params.get_child_value(0).get_uint32();
        const wsIdx = params.get_child_value(1).get_uint32();
        result = _handleMoveWindowToWorkspace(wid, wsIdx);
        break;
      }
      default:
        invocation.return_dbus_error(
          "org.jarvis.Desktop.Error",
          `Unknown method: ${methodName}`
        );
        return;
    }
    invocation.return_value(result);
  } catch (e) {
    logError(e, `JARVIS Desktop Bridge: ${methodName}`);
    invocation.return_dbus_error(
      "org.jarvis.Desktop.Error",
      `${methodName} failed: ${e.message}`
    );
  }
}

// ── Extension class ─────────────────────────────────────────────────

export default class JarvisDesktopExtension extends Extension {
  enable() {
    this._dbusId = null;
    this._busNameId = 0;

    const nodeInfo = Gio.DBusNodeInfo.new_for_xml(DBUS_IFACE);

    this._dbusId = Gio.DBus.session.register_object(
      "/org/jarvis/Desktop",
      nodeInfo.interfaces[0],
      _onMethodCall,
      null, // get_property
      null  // set_property
    );

    // Own the well-known bus name so clients can find us by name
    this._busNameId = Gio.bus_own_name_on_connection(
      Gio.DBus.session,
      "org.jarvis.Desktop",
      Gio.BusNameOwnerFlags.NONE,
      null, // name_acquired
      null  // name_lost
    );

    console.log("[JARVIS] Desktop Bridge enabled (D-Bus: org.jarvis.Desktop)");
  }

  disable() {
    if (this._busNameId) {
      Gio.bus_unown_name(this._busNameId);
      this._busNameId = 0;
    }
    if (this._dbusId) {
      Gio.DBus.session.unregister_object(this._dbusId);
      this._dbusId = null;
    }
    console.log("[JARVIS] Desktop Bridge disabled");
  }
}
