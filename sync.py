"""Bounded, coalescing background delivery. Never calls Tk or logs credentials."""

from __future__ import annotations

import copy
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from email.utils import parsedate_to_datetime

from . import VERSION


def server_url(value):
    value = value.strip().rstrip("/")
    if any(char.isspace() for char in value):
        raise ValueError("The EDHQ server URL cannot contain whitespace.")
    parsed = urllib.parse.urlsplit(value)
    # Reading port validates the syntax/range before a retrying worker sees it.
    parsed.port
    if (
        parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
    ):
        raise ValueError("Use the EDHQ website origin without a path or credentials.")
    local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme != "https" and not (parsed.scheme == "http" and local):
        raise ValueError("Use HTTPS, or HTTP on localhost for development.")
    if not parsed.hostname:
        raise ValueError("Enter a valid EDHQ server URL.")
    return value


@dataclass(frozen=True, repr=False)
class Delivery:
    server: str
    token: str
    payload: dict
    generation: int


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # A reverse-proxy redirect must never forward a bearer token elsewhere.
        raise urllib.error.HTTPError(req.full_url, code, "Redirect refused", headers, fp)


def retry_delay(value):
    """Accept Retry-After seconds or an HTTP date, bounded to one day."""
    if not value:
        return None
    try:
        delay = float(value)
    except (TypeError, ValueError):
        try:
            delay = parsedate_to_datetime(value).timestamp() - time.time()
        except (ValueError, TypeError, OverflowError):
            return None
    return min(86400, max(0, delay)) if delay == delay else None


def post_snapshot(delivery, timeout=8):
    payload = json.dumps(delivery.payload, separators=(",", ":"), allow_nan=False).encode("utf-8")
    request = urllib.request.Request(
        delivery.server + "/api/commander/sync",
        data=payload,
        method="POST",
        headers={
            "Authorization": "Bearer " + delivery.token,
            "Content-Type": "application/json",
            "User-Agent": "EDHQ-EDMC/" + VERSION,
        },
    )
    try:
        with urllib.request.build_opener(SafeRedirect()).open(request, timeout=timeout) as response:
            response.read(4096)
            return response.status, None
    except urllib.error.HTTPError as error:
        # Only expose a recognized code. Do not echo response text/private fields.
        code = None
        try:
            body = json.loads(error.read(4096))
            code = body.get("code") if isinstance(body, dict) else None
        except (ValueError, OSError):
            pass
        return (
            error.code,
            code,
            retry_delay(error.headers.get("Retry-After") if error.headers else None),
        )


class SyncWorker:
    def __init__(self, sender=post_snapshot, min_interval=30, retry_base=10, retry_max=300):
        self.sender = sender
        self.min_interval = min_interval
        self.retry_base = retry_base
        self.retry_max = retry_max
        self.condition = threading.Condition()
        self.pending = None
        self.generation = 0
        self.stopping = False
        self.blocked = False
        self.status = "Sync disabled"
        self.next_send = 0.0
        self.thread = threading.Thread(target=self._run, name="EDHQ sync", daemon=True)
        self.thread.start()

    def configure(self):
        """Invalidate queued work when credentials/commander/opt-in changes."""
        with self.condition:
            self.generation += 1
            self.pending = None
            self.next_send = 0.0
            self.blocked = False
            self.condition.notify_all()

    def submit(self, server, token, payload):
        with self.condition:
            if self.stopping or self.blocked:
                return
            self.pending = Delivery(
                server_url(server), token, copy.deepcopy(payload), self.generation
            )
            self.status = "Sync queued"
            self.condition.notify_all()

    def set_status(self, status):
        with self.condition:
            self.status = status

    def get_status(self):
        with self.condition:
            return self.status

    def stop(self, timeout=1.0):
        with self.condition:
            self.stopping = True
            self.pending = None
            self.condition.notify_all()
        self.thread.join(timeout=max(0, timeout))

    def _run(self):
        failures = 0
        while True:
            with self.condition:
                while not self.stopping and (
                    self.pending is None or time.monotonic() < self.next_send
                ):
                    wait = max(0.01, self.next_send - time.monotonic()) if self.pending else None
                    self.condition.wait(wait)
                if self.stopping:
                    return
                delivery = self.pending
                self.pending = None
                self.status = "Syncing"
            try:
                result = self.sender(delivery)
                status = result[0]
                retry_after = result[2] if len(result) > 2 else None
            except Exception:
                # urllib/OS exception strings can contain sensitive URLs. Never display them.
                status = 0
                retry_after = None
            with self.condition:
                if self.stopping:
                    return
                if delivery.generation != self.generation:
                    failures = 0
                    continue
                if 200 <= status < 300:
                    failures = 0
                    self.status = "Synced to EDHQ"
                    self.next_send = time.monotonic() + self.min_interval
                elif status in (401, 403, 409, 422) or (
                    400 <= status < 500 and status not in (408, 429)
                ):
                    failures = 0
                    self.pending = None
                    self.status = (
                        "Commander mismatch: use this commander's own EDHQ account/token"
                        if status == 409
                        else "Token rejected: create a new sync token in EDHQ"
                        if status in (401, 403)
                        else "Sync rejected: check plugin/server versions"
                    )
                    # New submissions stay blocked until explicit reconfiguration.
                    self.next_send = float("inf")
                    self.blocked = True
                else:
                    failures += 1
                    self.status = "EDHQ unavailable; retrying automatically"
                    self.pending = self.pending or delivery
                    backoff = min(self.retry_max, self.retry_base * 2 ** min(failures - 1, 8))
                    self.next_send = time.monotonic() + max(backoff, retry_after or 0)
                self.condition.notify_all()
