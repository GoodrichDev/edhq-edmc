"""Isolated official EDMC loader/Tk smoke, without player config or networking.

Supply a clean source checkout of an official EDMC release. Only plug.py and
myNotebook.py are imported from it; config/CAPI/logging/translation services are
isolated in-memory substitutes. No installed EDMC profile is opened.
"""

from __future__ import annotations

import argparse
import importlib.util
import logging
import sys
import tempfile
import tkinter as tk
import types
import urllib.request
from pathlib import Path
from tkinter import ttk


class MemoryConfig:
    def __init__(self):
        self.values = {}
        self.skip_timecheck = False
        self.shutting_down = False

    def get_int(self, key, default=0):
        return int(self.values.get(key, default))

    def get_str(self, key, default=""):
        return self.values.get(key, default)

    def set(self, key, value):
        self.values[key] = value


def load_source(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--edmc-source", required=True, type=Path)
    args = parser.parse_args()
    source = args.edmc_source.resolve()
    plugin_path = Path(__file__).resolve().parents[1]
    memory = MemoryConfig()
    config_module = types.ModuleType("config")
    config_module.config = memory
    sys.modules["config"] = config_module
    companion = types.ModuleType("companion")
    companion.SERVER_LIVE = "live.example.invalid"
    companion.SERVER_LEGACY = "legacy.example.invalid"
    sys.modules["companion"] = companion
    translation = types.ModuleType("l10n")
    translation.translations = types.SimpleNamespace(tl=lambda value: value)
    sys.modules["l10n"] = translation
    logger_module = types.ModuleType("EDMCLogging")
    logger_module.get_main_logger = lambda: logging.getLogger("edhq-smoke")
    sys.modules["EDMCLogging"] = logger_module
    # Fail closed even if startup or a fixture unexpectedly tries to upload.
    network_calls = []

    def deny_network(*args, **kwargs):
        network_calls.append(True)
        raise AssertionError("Network is forbidden in the isolated smoke test")

    urllib.request.OpenerDirector.open = deny_network
    root = tk.Tk()
    root.withdraw()
    try:
        nb = load_source("myNotebook", source / "myNotebook.py")
        plug = load_source("plug", source / "plug.py")
        # Exercise package imports exactly as EDMC does, using an edhq folder
        # even when the standalone source repository has another directory name.
        with tempfile.TemporaryDirectory(prefix="edhq-edmc-smoke-") as temp:
            import shutil

            target = Path(temp) / "edhq"
            shutil.copytree(
                plugin_path, target, ignore=shutil.ignore_patterns(".git", "__pycache__", "dist")
            )
            sys.path.insert(0, temp)
            plugin = plug.Plugin("edhq", target / "load.py", logging.getLogger("edhq-smoke"))
            plug.PLUGINS.append(plugin)
            assert plugin.name == "ED Headquarters"
            assert plugin.get_version() == "0.2.0"
            main_frame = ttk.Frame(root)
            app = plugin.get_app(main_frame)
            assert isinstance(app, tuple) and len(app) == 2
            notebook = nb.Notebook(root)
            prefs = plugin.get_prefs(notebook, "Fixture Commander", False)
            assert isinstance(prefs, nb.Frame)
            notebook.add(prefs, text=plugin.name)
            module = plugin.module
            assert module.preferences["enabled"].get() == 0
            assert all(value.get() == 0 for value, _ in module.preferences["options"].values())
            module.preferences["token"].set("synthetic-fixture-token-A")
            plug.notify_prefs_changed("Fixture Commander", False)
            assert module.settings("Fixture Commander")[2] == "synthetic-fixture-token-A"
            plug.notify_prefs_cmdr_changed("Another Commander", False)
            assert module.preferences["token"].get() == ""
            assert module.preferences["enabled"].get() == 0
            plug.notify_prefs_changed("Another Commander", False)
            assert module.settings("Fixture Commander")[2] == "synthetic-fixture-token-A"
            assert module.settings("Another Commander")[2] == ""
            assert (
                plug.notify_journal_entry(
                    "Fixture Commander",
                    False,
                    None,
                    None,
                    {"event": "Rank"},
                    {"FID": "F100", "GameVersion": "4.2", "Credits": 123},
                )
                is None
            )
            root.update_idletasks()
            root.update()
            worker = module.worker
            assert plug.notify_stop() is None
            assert not worker.thread.is_alive()
            assert network_calls == []
            print(
                "PASS: official release loader, version, real Tk app/preferences, "
                "commander token isolation and shutdown; no network/player config"
            )
    finally:
        root.destroy()


if __name__ == "__main__":
    main()
