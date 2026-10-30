"""Durable opt-in delivery. Credentials remain in memory, never in the outbox."""

from __future__ import annotations

import copy
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
import time
import urllib.error
import urllib.request

from . import VERSION
from .model_v2 import BASE_SECTIONS, HISTORY
from .sync import Delivery, SafeRedirect, post_snapshot, server_url

MAX_BODY = 768 * 1024
MAX_QUEUE_BYTES = 128 * 1024 * 1024
MAX_QUEUE_ROWS = 50_000
IDENTITY = {
    "ships": "game_id",
    "reputation": "id",
    "engineers": "id",
    "permits": "symbol",
    "inventory": "id",
    "cargo": "id",
    "stored_modules": "id",
    "loadouts": "game_id",
    "fleet_locations": "game_id",
    "equipment": "id",
    "community_goals": "id",
}


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def capabilities(server):
    request = urllib.request.Request(
        server + "/api/commander/capabilities", headers={"User-Agent": "EDHQ-EDMC/" + VERSION}
    )
    try:
        with urllib.request.build_opener(SafeRedirect()).open(request, timeout=8) as response:
            body = response.read(128 * 1024 + 1)
            if len(body) > 128 * 1024:
                raise ValueError("Oversized capability response")
            value = json.loads(body)
            if not isinstance(value, dict) or not isinstance(value.get("sync_versions"), list):
                raise ValueError("Invalid capability response")
            return value
    except urllib.error.HTTPError as error:
        if error.code in (404, 410):
            return {"sync_versions": [1]}
        raise


class Outbox:
    """Owned by one worker lock. SQLite transactions protect restart boundaries."""

    def __init__(self, path):
        if str(path) != ":memory:":
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.parent.chmod(0o700)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
         CREATE TABLE IF NOT EXISTS pending(
          id INTEGER PRIMARY KEY,target TEXT NOT NULL,section TEXT NOT NULL,key TEXT NOT NULL,
          value TEXT NOT NULL,cursor INTEGER NOT NULL DEFAULT 0,
          UNIQUE(target,section,key));
        """)
        if str(path) != ":memory:":
            path.chmod(0o600)

    def add(self, target, payload):
        with self.db:
            for section, observation in payload["sections"].items():
                if section in HISTORY:
                    for item in observation["data"]["items"]:
                        value = encoded({"observed_at": item["at"], "data": {"items": [item]}})
                        self.db.execute(
                            "INSERT OR IGNORE INTO pending(target,section,key,value) "
                            "VALUES(?,?,?,?)",
                            (target, section, item["event_id"], value),
                        )
                else:
                    merged = copy.deepcopy(observation)
                    previous = self.db.execute(
                        "SELECT value FROM pending WHERE target=? AND section=? AND key='snapshot'",
                        (target, section),
                    ).fetchone()
                    data = merged["data"]
                    if previous and section in IDENTITY and not data.get("complete"):
                        old = json.loads(previous[0])["data"]
                        identity = IDENTITY[section]
                        items = {item[identity]: item for item in old["items"]}
                        for key in data.get("removed", []):
                            items.pop(key, None)
                        for item in data["items"]:
                            key = item[identity]
                            items[key] = {**items.get(key, {}), **item}
                        data["items"] = list(items.values())
                        data["complete"] = bool(old.get("complete"))
                        removed = set(old.get("removed", [])) | set(data.get("removed", []))
                        removed.difference_update(item[identity] for item in data["items"])
                        if removed:
                            data["removed"] = sorted(removed)
                    merged_json = encoded(merged)
                    if previous and previous[0] == merged_json:
                        continue
                    self.db.execute(
                        """INSERT INTO pending(target,section,key,value) VALUES(?,?,'snapshot',?)
                     ON CONFLICT(target,section,key) DO UPDATE SET value=excluded.value,cursor=0""",
                        (target, section, merged_json),
                    )
            count, size = self.db.execute(
                "SELECT count(*),coalesce(sum(length(value)),0) FROM pending"
            ).fetchone()
            if count > MAX_QUEUE_ROWS or size > MAX_QUEUE_BYTES:
                raise OverflowError("EDHQ outbox full; restore sync before recording more history")

    def restrict(self, target, allowed):
        with self.db:
            for row_id, section, raw in list(
                self.db.execute("SELECT id,section,value FROM pending WHERE target=?", (target,))
            ):
                if section not in allowed:
                    self.db.execute("DELETE FROM pending WHERE id=?", (row_id,))
                    continue
                value = json.loads(raw)
                data = value["data"]
                if section == "credits" and "finances" not in allowed:
                    data.pop("assets", None)
                    data.pop("loan", None)
                if section == "carrier" and "location" not in allowed:
                    data.pop("system", None)
                    data.pop("system_id", None)
                if section == "stored_modules" and not {"location", "fleet_locations"} & set(
                    allowed
                ):
                    for item in data["items"]:
                        for key in (
                            "system",
                            "system_id",
                            "station",
                            "market_id",
                            "transfer_time",
                            "transfer_price",
                        ):
                            item.pop(key, None)
                sanitized = encoded(value)
                if sanitized != raw:
                    self.db.execute(
                        "UPDATE pending SET value=?,cursor=0 WHERE id=?", (sanitized, row_id)
                    )

    def prepare(self, target, commander, version, limit=MAX_BODY):
        payload = {"version": version, "commander": commander, "sections": {}}
        acknowledgements = []
        for row_id, section, raw, cursor in self.db.execute(
            "SELECT id,section,value,cursor FROM pending WHERE target=? ORDER BY id LIMIT 1500",
            (target,),
        ):
            if version == 1 and section not in BASE_SECTIONS:
                continue
            value = json.loads(raw)
            if version == 1 and section == "ships" and value["data"].get("removed"):
                continue
            original = copy.deepcopy(payload["sections"].get(section))
            if section in HISTORY:
                existing = payload["sections"].setdefault(
                    section, {"observed_at": value["observed_at"], "data": {"items": []}}
                )
                if len(existing["data"]["items"]) >= 500:
                    continue
                existing["data"]["items"].extend(value["data"]["items"])
                next_cursor = None
            elif section in IDENTITY:
                items = value["data"]["items"]
                take = min(100, len(items) - cursor)
                while True:
                    candidate = copy.deepcopy(value)
                    candidate["data"]["items"] = items[cursor : cursor + take]
                    candidate["data"]["complete"] = bool(value["data"]["complete"] and cursor == 0)
                    if cursor:
                        candidate["data"].pop("removed", None)
                    at = datetime.fromisoformat(value["observed_at"].replace("Z", "+00:00"))
                    candidate["observed_at"] = (
                        (at + timedelta(microseconds=cursor))
                        .isoformat(timespec="microseconds")
                        .replace("+00:00", "Z")
                    )
                    payload["sections"][section] = candidate
                    if len(encoded(payload).encode()) <= limit or take <= 1:
                        break
                    take //= 2
                next_cursor = cursor + take if cursor + take < len(items) else None
            else:
                if version == 1 and section == "credits":
                    value["data"].pop("loan", None)
                payload["sections"][section] = value
                next_cursor = None
            if len(encoded(payload).encode()) > limit:
                if original is None:
                    payload["sections"].pop(section, None)
                else:
                    payload["sections"][section] = original
                if not acknowledgements:
                    raise OverflowError("One normalized section exceeds the server payload limit")
                break
            acknowledgements.append((row_id, raw, next_cursor))
        return (payload, acknowledgements) if acknowledgements else (None, [])

    def acknowledge(self, rows):
        with self.db:
            for identity, previous, next_cursor in rows:
                if next_cursor is None:
                    self.db.execute(
                        "DELETE FROM pending WHERE id=? AND value=?", (identity, previous)
                    )
                else:
                    self.db.execute(
                        "UPDATE pending SET cursor=? WHERE id=? AND value=?",
                        (next_cursor, identity, previous),
                    )


class SyncWorker:
    def __init__(self, path, sender=post_snapshot, discover=capabilities, min_interval=30):
        self.outbox = Outbox(path)
        self.sender, self.discover, self.min_interval = sender, discover, min_interval
        self.condition = threading.Condition(threading.RLock())
        self.current = None
        self.generation = 0
        self.stopping = False
        self.blocked = False
        self.status = "Sync disabled"
        self.next_send = 0.0
        self.supported = {}
        self.thread = threading.Thread(target=self._run, name="EDHQ durable sync", daemon=True)
        self.thread.start()

    def configure(self):
        with self.condition:
            self.generation += 1
            self.current = None
            self.blocked = False
            self.next_send = 0.0
            self.condition.notify_all()

    def set_status(self, value):
        with self.condition:
            self.status = value

    def get_status(self):
        with self.condition:
            return self.status

    def submit(self, server, token, payload, allowed=None):
        server = server_url(server)
        target = hashlib.sha256(
            encoded([server, token, payload["commander"]["fid"]]).encode()
        ).hexdigest()
        with self.condition:
            if self.stopping:
                return False
            # No bearer token is written to SQLite; rotating one creates a new
            # delivery channel. A different commander/server never drains it.
            permitted = allowed if allowed is not None else set(payload["sections"])
            selected = {
                **payload,
                "sections": {
                    name: value for name, value in payload["sections"].items() if name in permitted
                },
            }
            self.outbox.add(target, selected)
            self.outbox.restrict(target, permitted)
            self.current = (target, server, token, copy.deepcopy(payload["commander"]))
            self.status = "Sync queued" if not self.blocked else self.status
            self.condition.notify_all()
            return True

    def stop(self, timeout=1.0):
        with self.condition:
            self.stopping = True
            self.current = None
            self.condition.notify_all()
        self.thread.join(timeout=max(0, timeout))
        # The worker closes SQLite after an in-flight bounded request finishes.

    def _run(self):
        failures = 0
        try:
            while True:
                with self.condition:
                    while not self.stopping and (
                        not self.current or self.blocked or time.monotonic() < self.next_send
                    ):
                        self.condition.wait(timeout=1)
                    if self.stopping:
                        return
                    target, server, token, commander = self.current
                    generation = self.generation
                try:
                    known = self.supported.get(server)
                    if not known or time.monotonic() - known[0] > 600:
                        value = self.discover(server)
                        known = (time.monotonic(), value)
                        self.supported[server] = known
                    version = 2 if 2 in known[1].get("sync_versions", []) else 1
                    limit = min(MAX_BODY, int(known[1].get("max_body_bytes", MAX_BODY)))
                    with self.condition:
                        if self.current is None or self.generation != generation:
                            continue
                        payload, rows = self.outbox.prepare(target, commander, version, limit)
                        if payload is None:
                            self.status = (
                                "Basic sync complete; server upgrade needed for additional data"
                                if version == 1
                                else "Synced to EDHQ"
                            )
                            self.next_send = time.monotonic() + 30
                            continue
                        self.status = "Syncing"
                    result = self.sender(Delivery(server, token, payload, generation))
                    status, retry_after = result[0], result[2] if len(result) > 2 else None
                except OverflowError:
                    with self.condition:
                        self.status = (
                            "Outbox payload too large; update plugin/server before retrying"
                        )
                        self.blocked = True
                    continue
                except Exception:
                    status, retry_after = 0, None
                with self.condition:
                    # A response can acknowledge only its exact saved rows,
                    # even if a callback replaced a snapshot during the upload.
                    if 200 <= status < 300:
                        self.outbox.acknowledge(rows)
                    if self.generation != generation:
                        continue
                    if 200 <= status < 300:
                        failures = 0
                        self.status = "Synced to EDHQ"
                        self.next_send = time.monotonic() + self.min_interval
                    elif status in (401, 403, 409, 422) or (
                        400 <= status < 500 and status not in (408, 429)
                    ):
                        self.blocked = True
                        self.status = (
                            "Commander mismatch: check this commander's token"
                            if status == 409
                            else "Token rejected: create a new token"
                            if status in (401, 403)
                            else "Sync rejected: check plugin/server versions"
                        )
                    else:
                        failures += 1
                        self.status = "EDHQ unavailable; saved data will retry automatically"
                        self.next_send = time.monotonic() + max(
                            min(300, 10 * 2 ** min(failures - 1, 5)), retry_after or 0
                        )
        finally:
            with self.condition:
                self.outbox.db.close()
