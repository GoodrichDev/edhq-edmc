"""EDMarketConnector callbacks; Tk is used only on EDMC's main thread."""

from __future__ import annotations

import hashlib
import tkinter as tk
import webbrowser
from tkinter import ttk

import myNotebook as nb
from config import config

from . import VERSION as VERSION
from .model import Collector
from .sync import SyncWorker, server_url

PLUGIN_NAME = "ED Headquarters"
DEFAULT_SERVER = "https://edhq.io"
collector = Collector()
worker = None
active = None
last_sent = None
status_label = None
status_after = None
preferences = None


def setting_key(commander, field):
    """Separate credentials without storing commander names in setting keys."""
    digest = hashlib.sha256(commander.casefold().encode("utf-8")).hexdigest()[:24]
    return "edhq_" + digest + "_" + field


def settings(commander):
    if not commander:
        return False, DEFAULT_SERVER, ""
    enabled = bool(config.get_int(setting_key(commander, "enabled"), default=0))
    server = config.get_str(setting_key(commander, "server"), default=DEFAULT_SERVER)
    token = config.get_str(setting_key(commander, "token"), default="")
    return enabled, server, token


def plugin_start3(plugin_dir):
    """Start one daemon worker; no file scan or network request on startup."""
    global worker
    worker = SyncWorker()
    return PLUGIN_NAME


def plugin_stop():
    global status_after, status_label, worker
    if status_label is not None and status_after is not None:
        try:
            status_label.after_cancel(status_after)
        except tk.TclError:
            pass
    status_label = None
    status_after = None
    if worker:
        worker.stop()
    worker = None


def _poll_status():
    global status_after
    if status_label is None or worker is None:
        return
    try:
        status_label.configure(text=worker.get_status())
        status_after = status_label.after(1000, _poll_status)
    except tk.TclError:
        status_after = None


def plugin_app(parent):
    global status_label
    label = ttk.Label(parent, text="EDHQ:")
    status_label = ttk.Label(parent, text="Sync disabled")
    _poll_status()
    return label, status_label


def _activate(commander):
    global active, last_sent
    normalized = commander.casefold() if isinstance(commander, str) and commander else None
    if active != normalized:
        active = normalized
        last_sent = None
        if worker:
            worker.configure()


def _queue_snapshot():
    global last_sent
    if worker is None:
        return
    enabled, server, token = settings(collector.commander)
    if not enabled:
        worker.set_status("Sync disabled")
        return
    if not token:
        worker.set_status("Add this commander's EDHQ sync token in settings")
        return
    snapshot = collector.snapshot()
    if not snapshot:
        worker.set_status(
            "Commander changed identity; restart EDMC and check the account"
            if collector.identity_error
            else "Waiting for live commander data and Frontier ID"
        )
        return
    if snapshot == last_sent:
        return
    try:
        worker.submit(server, token, snapshot)
        last_sent = snapshot
    except ValueError:
        worker.set_status("Invalid server URL; check EDHQ settings")


def journal_entry(cmdr, is_beta, system, station, entry, state):
    """Use EDMC's state, never read journals or send raw events."""
    _activate(cmdr)
    accepted = collector.journal(cmdr, is_beta, entry, state)
    if accepted:
        _queue_snapshot()
    elif worker and settings(cmdr)[0]:
        if is_beta or collector.blocked:
            worker.configure()
            worker.set_status("Sync paused outside the live personal commander session")
        elif not collector.fid:
            worker.set_status("Waiting for live commander data and Frontier ID")


def cmdr_data(data, is_beta):
    """Only accept a live CAPI response belonging to the active journal CMDR."""
    from companion import SERVER_LIVE

    if getattr(data, "source_host", None) != SERVER_LIVE:
        return
    if collector.capi(data, is_beta=is_beta, source_live=True):
        _queue_snapshot()


def cmdr_data_legacy(data, is_beta):
    """Legacy data must never update a live-galaxy commander profile."""
    return None


def plugin_prefs(parent, cmdr, is_beta):
    global preferences
    frame = nb.Frame(parent)
    frame.columnconfigure(1, weight=1)
    preferences = {
        "commander": cmdr,
        "enabled": tk.IntVar(master=frame, value=0),
        "server": tk.StringVar(master=frame, value=DEFAULT_SERVER),
        "token": tk.StringVar(master=frame, value=""),
    }
    nb.Label(frame, text="Commander").grid(row=0, column=0, sticky=tk.W, padx=10, pady=5)
    preferences["name"] = nb.Label(frame, text=cmdr or "Not logged in")
    preferences["name"].grid(row=0, column=1, sticky=tk.W, padx=10, pady=5)
    preferences["check"] = nb.Checkbutton(
        frame,
        text="Sync ranks, credits, owned ships and exploration statistics to EDHQ",
        variable=preferences["enabled"],
    )
    preferences["check"].grid(row=1, column=0, columnspan=2, sticky=tk.W, padx=10, pady=5)
    for row, label, field in ((2, "EDHQ server", "server"), (3, "Commander sync token", "token")):
        nb.Label(frame, text=label).grid(row=row, column=0, sticky=tk.W, padx=10, pady=5)
        widget = nb.EntryMenu(
            frame, textvariable=preferences[field], width=48, show="*" if field == "token" else ""
        )
        widget.grid(row=row, column=1, sticky=tk.EW, padx=10, pady=5)
        preferences[field + "_widget"] = widget
    nb.Label(
        frame,
        text=(
            "Create a token in your EDHQ commander account. "
            "Public visibility is controlled on EDHQ."
        ),
        wraplength=520,
    ).grid(row=4, column=0, columnspan=2, sticky=tk.W, padx=10, pady=5)
    nb.Button(
        frame,
        text="Open EDHQ account",
        command=lambda: webbrowser.open("https://edhq.io/#commanders"),
    ).grid(
        row=5,
        column=0,
        columnspan=2,
        sticky=tk.W,
        padx=10,
        pady=5,
    )
    prefs_cmdr_changed(cmdr, is_beta)
    return frame


def prefs_cmdr_changed(cmdr, is_beta):
    if preferences is None:
        return
    preferences["commander"] = cmdr
    enabled, server, token = settings(cmdr)
    preferences["enabled"].set(int(enabled))
    preferences["server"].set(server)
    preferences["token"].set(token)
    preferences["name"].configure(text=cmdr or "Not logged in")
    state = tk.NORMAL if cmdr and not is_beta else tk.DISABLED
    for key in ("check", "server_widget", "token_widget"):
        preferences[key].configure(state=state)


def prefs_changed(cmdr, is_beta):
    global last_sent
    if not preferences or not cmdr or is_beta:
        return
    if str(preferences["commander"]).casefold() != cmdr.casefold():
        return
    try:
        server = server_url(preferences["server"].get())
    except ValueError:
        if worker:
            worker.set_status("Invalid server URL; settings were not saved")
        return
    token = preferences["token"].get().strip()
    if len(token) > 1024 or any(char.isspace() for char in token):
        if worker:
            worker.set_status("Invalid sync token; settings were not saved")
        return
    config.set(setting_key(cmdr, "enabled"), preferences["enabled"].get())
    config.set(setting_key(cmdr, "server"), server)
    config.set(setting_key(cmdr, "token"), token)
    if worker:
        worker.configure()
    last_sent = None
    _queue_snapshot()
