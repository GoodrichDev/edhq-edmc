"""Synthetic callback and worker tests; no player data or network access."""

import copy
import importlib.util
import json
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "edhq", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
)
if "edhq" not in sys.modules:
    MODULE = importlib.util.module_from_spec(SPEC)
    sys.modules["edhq"] = MODULE
    SPEC.loader.exec_module(MODULE)

from edhq.model import Collector
from edhq.sync import Delivery, SafeRedirect, SyncWorker, post_snapshot, retry_delay, server_url

TIME = "2026-10-28T12:00:00Z"


def journal(collector, state=None, entry=None, commander="Fixture Commander", beta=False):
    return collector.journal(
        commander,
        beta,
        entry or {"event": "Rank", "timestamp": TIME},
        {"FID": "F100", "GameVersion": "4.2", **(state or {})},
    )


class CAPI(dict):
    request_cmdr = "Fixture Commander"


class CollectorTests(unittest.TestCase):
    def test_missing_fid_omits_everything(self):
        collector = Collector()
        self.assertFalse(journal(collector, {"FID": None, "Credits": 100}))
        self.assertIsNone(collector.snapshot())

    def test_partial_state_does_not_invent_zero_values(self):
        collector = Collector()
        journal(collector, {"Credits": 100})
        self.assertEqual(set(collector.snapshot()["sections"]), {"credits"})
        journal(collector, {"Credits": None})
        self.assertEqual(collector.snapshot()["sections"]["credits"]["data"], {"balance": 100})

    def test_ranks_tuple_contains_rank_not_age(self):
        collector = Collector()
        journal(collector, {"Rank": {"Combat": (8, 1023), "Explore": [5, 80], "Soldier": (3, 50)}})
        self.assertEqual(
            collector.snapshot()["sections"]["ranks"]["data"],
            {
                "combat": {"rank": 8},
                "exploration": {"rank": 5},
                "mercenary": {"rank": 3},
            },
        )

    def test_unknown_and_invalid_fields_are_stripped(self):
        collector = Collector()
        journal(
            collector,
            {
                "Credits": True,
                "Rank": {"Combat": (-1, 10), "Friends": "private"},
                "Friends": {"Real person"},
            },
        )
        self.assertIsNone(collector.snapshot())

    def test_partial_ranks_keep_previous_known_fields(self):
        collector = Collector()
        journal(collector, {"Rank": {"Combat": (3, 0)}})
        journal(collector, {"Rank": {"Trade": (4, 0)}})
        self.assertEqual(
            set(collector.snapshot()["sections"]["ranks"]["data"]), {"combat", "trade"}
        )

    def test_replay_uses_cumulative_statistics_not_jump_increments(self):
        collector = Collector()
        stats = {
            "Exploration": {"Total_Hyperspace_Jumps": 123, "Total_Hyperspace_Distance": 4567.5}
        }
        journal(collector, {"Statistics": stats})
        before = collector.snapshot()
        for _ in range(4):
            journal(
                collector,
                {"Statistics": stats},
                {"event": "FSDJump", "timestamp": TIME, "JumpDist": 70},
            )
        self.assertEqual(before, collector.snapshot())

    def test_statistics_and_exobiology_allowlist(self):
        collector = Collector()
        journal(
            collector,
            entry={
                "event": "Statistics",
                "timestamp": TIME,
                "Exploration": {"Systems_Visited": 42, "Highest_Payout": 500, "Secrets": "no"},
                "Exobiology": {"OnFoot_Organics_Analysed": 12, "OnFoot_Organic_Data_Profits": 9000},
                "Bank_Account": {"Current_Wealth": 1234},
            },
        )
        values = collector.snapshot()["sections"]["exploration"]["data"]
        self.assertEqual(
            values,
            {
                "systems_visited": 42,
                "highest_payout": 500,
                "onfoot_organics_analysed": 12,
                "organic_data_profits": 9000,
            },
        )

    def test_commander_switch_resets_snapshots_and_fid(self):
        collector = Collector()
        journal(collector, {"Credits": 100})
        self.assertFalse(journal(collector, {"FID": None}, commander="Another Commander"))
        self.assertIsNone(collector.snapshot())
        journal(
            collector, {"FID": "F200", "Rank": {"Combat": (1, 0)}}, commander="Another Commander"
        )
        self.assertNotIn("credits", collector.snapshot()["sections"])
        self.assertEqual(collector.snapshot()["commander"]["fid"], "F200")

    def test_changed_fid_does_not_relink_profile(self):
        collector = Collector()
        journal(collector, {"Credits": 100})
        self.assertFalse(journal(collector, {"FID": "F999", "Credits": 999}))
        self.assertTrue(collector.identity_error)
        self.assertIsNone(collector.snapshot())

    def test_entry_commander_must_match_callback(self):
        collector = Collector()
        self.assertFalse(
            journal(
                collector,
                entry={
                    "event": "LoadGame",
                    "timestamp": TIME,
                    "Commander": "Wrong",
                    "Ship": "anaconda",
                    "GameMode": "Solo",
                },
            )
        )
        self.assertIsNone(collector.snapshot())

    def test_legacy_beta_cqc_and_multicrew_are_excluded(self):
        for state, entry, beta in [
            ({"GameVersion": "3.8"}, None, False),
            ({"Captain": "Other Commander"}, None, False),
            ({}, None, True),
            ({}, {"event": "LoadGame", "timestamp": TIME, "GameMode": "CQC"}, False),
        ]:
            with self.subTest(state=state, entry=entry, beta=beta):
                collector = Collector()
                self.assertFalse(journal(collector, {"Credits": 123, **state}, entry, beta=beta))
                self.assertIsNone(collector.snapshot())

    def test_stale_snapshots_do_not_roll_state_back(self):
        collector = Collector()
        journal(collector, {"Credits": 200})
        journal(collector, {"Credits": 100}, {"event": "Rank", "timestamp": "2026-10-27T12:00:00Z"})
        self.assertEqual(collector.snapshot()["sections"]["credits"]["data"]["balance"], 200)

    def test_distinct_changes_within_one_second_have_ordered_timestamps(self):
        collector = Collector()
        journal(collector, {"Credits": 100})
        first = collector.snapshot()["sections"]["credits"]["observed_at"]
        journal(collector, {"Credits": 200})
        second = collector.snapshot()["sections"]["credits"]["observed_at"]
        self.assertLess(first, second)
        self.assertTrue(second.endswith(".000001Z"))
        journal(collector, {"Credits": 200})
        self.assertEqual(second, collector.snapshot()["sections"]["credits"]["observed_at"])

    def test_unchanged_authoritative_response_still_guards_against_older_data(self):
        collector = Collector()
        journal(collector, {"Credits": 100})
        before = collector.snapshot()
        with patch("edhq.model.utc_now", return_value="2026-10-28T12:00:10Z"):
            collector.capi(CAPI(commander={"name": "Fixture Commander", "credits": 100}))
        self.assertEqual(before, collector.snapshot())
        journal(collector, {"Credits": 50}, {"event": "Rank", "timestamp": "2026-10-28T12:00:05Z"})
        self.assertEqual(collector.snapshot()["sections"]["credits"]["data"]["balance"], 100)

    def test_multibyte_text_stays_within_backend_byte_limits(self):
        collector = Collector()
        journal(
            collector,
            {"ShipID": 1, "ShipType": "anaconda", "ShipName": "界" * 100, "ShipIdent": "界" * 100},
            commander="界" * 100,
        )
        snapshot = collector.snapshot()
        self.assertLessEqual(len(snapshot["commander"]["name"].encode("utf-8")), 100)
        ship = snapshot["sections"]["ships"]["data"]["items"][0]
        self.assertLessEqual(len(ship["name"].encode("utf-8")), 100)
        self.assertLessEqual(len(ship["ident"].encode("utf-8")), 32)

    def test_ship_name_can_be_cleared_without_erasing_unknown_ident(self):
        collector = Collector()
        journal(
            collector,
            {"ShipID": 1, "ShipType": "anaconda", "ShipName": "Old name", "ShipIdent": "TEST"},
        )
        journal(collector, {"ShipID": 1, "ShipType": "anaconda", "ShipName": "", "ShipIdent": None})
        ship = collector.snapshot()["sections"]["ships"]["data"]["items"][0]
        self.assertEqual(ship["name"], "")
        self.assertEqual(ship["ident"], "TEST")

    def test_ship_is_partial_and_private_fields_are_omitted(self):
        collector = Collector()
        journal(
            collector,
            {
                "ShipID": 100,
                "ShipType": "anaconda",
                "ShipName": "Fixture",
                "ShipIdent": "TEST",
                "FuelCapacity": {"Main": 64},
                "Rebuy": 123,
                "Modules": {"Private": "data"},
            },
        )
        ships = collector.snapshot()["sections"]["ships"]["data"]
        self.assertFalse(ships["complete"])
        self.assertEqual(
            ships["items"],
            [
                {
                    "game_id": "100",
                    "ship_type": "anaconda",
                    "name": "Fixture",
                    "ident": "TEST",
                    "current": True,
                }
            ],
        )

    def test_storedships_reconciles_fleet_and_current_ship(self):
        collector = Collector()
        journal(
            collector,
            {"ShipID": 1, "ShipType": "anaconda"},
            {
                "event": "StoredShips",
                "timestamp": TIME,
                "ShipsHere": [
                    {"ShipID": 2, "ShipType": "cobramkiii", "Name": "Stored", "TransferPrice": 500}
                ],
                "ShipsRemote": [{"ShipID": 3, "ShipType": "sidewinder", "StarSystem": "Private"}],
            },
        )
        ships = collector.snapshot()["sections"]["ships"]["data"]
        self.assertTrue(ships["complete"])
        self.assertEqual([ship["game_id"] for ship in ships["items"]], ["1", "2", "3"])
        self.assertNotIn("Private", json.dumps(ships))

    def test_shipyard_does_not_become_owned_fleet(self):
        collector = Collector()
        journal(
            collector,
            entry={
                "event": "Shipyard",
                "timestamp": TIME,
                "Ships": [{"ShipID": 99, "ShipType": "anaconda"}],
            },
        )
        self.assertIsNone(collector.snapshot())

    def test_capi_fleets_list_and_dictionary(self):
        for ships in (
            [{"id": 1, "name": "anaconda"}, {"id": 5, "name": "cobramkiii"}],
            {"1": {"id": 1, "name": "anaconda"}, "5": {"id": 5, "name": "cobramkiii"}},
        ):
            with self.subTest(ships=ships):
                collector = Collector()
                journal(collector)
                data = CAPI(
                    commander={"name": "Fixture Commander", "currentShipId": 5}, ships=ships
                )
                self.assertTrue(collector.capi(data))
                fleet = collector.snapshot()["sections"]["ships"]["data"]
                self.assertTrue(fleet["complete"])
                self.assertEqual([ship["current"] for ship in fleet["items"]], [False, True])

    def test_capi_delayed_other_commander_and_no_fid_are_ignored(self):
        data = CAPI(commander={"name": "Fixture Commander", "credits": 10})
        collector = Collector()
        self.assertFalse(collector.capi(data))
        journal(collector)
        data.request_cmdr = "Previous Commander"
        self.assertFalse(collector.capi(data))
        data.request_cmdr = "Fixture Commander"
        data["commander"]["name"] = "Another Commander"
        self.assertFalse(collector.capi(data))

    def test_capi_current_only_never_deletes_owned_ships(self):
        collector = Collector()
        journal(collector, {"ShipID": 1, "ShipType": "anaconda"})
        collector.capi(
            CAPI(
                commander={"name": "Fixture Commander"},
                ship={"id": 2, "name": "cobramkiii"},
                lastStarport={"ships": [{"id": 99, "name": "sidewinder"}]},
            )
        )
        fleet = collector.snapshot()["sections"]["ships"]["data"]
        self.assertFalse(fleet["complete"])
        self.assertEqual([ship["game_id"] for ship in fleet["items"]], ["1", "2"])

    def test_malformed_fleet_never_removes_old_ships(self):
        collector = Collector()
        journal(collector, {"ShipID": 1, "ShipType": "anaconda"})
        collector.capi(CAPI(commander={"name": "Fixture Commander"}, ships=[None]))
        self.assertEqual(len(collector.snapshot()["sections"]["ships"]["data"]["items"]), 1)

    def test_current_ship_update_after_full_fleet_is_still_partial(self):
        collector = Collector()
        journal(collector)
        with patch("edhq.model.utc_now", return_value=TIME):
            collector.capi(
                CAPI(
                    commander={"name": "Fixture Commander", "currentShipId": 1},
                    ships=[{"id": 1, "name": "anaconda"}, {"id": 2, "name": "sidewinder"}],
                )
            )
        self.assertTrue(collector.snapshot()["sections"]["ships"]["data"]["complete"])
        journal(
            collector,
            {"ShipID": 2, "ShipType": "sidewinder"},
            {
                "event": "ShipyardSwap",
                "timestamp": "2026-10-28T12:00:01Z",
            },
        )
        self.assertFalse(collector.snapshot()["sections"]["ships"]["data"]["complete"])

    def test_snapshot_is_detached(self):
        collector = Collector()
        journal(collector, {"Credits": 100})
        snapshot = collector.snapshot()
        snapshot["sections"]["credits"]["data"]["balance"] = 999
        self.assertEqual(collector.snapshot()["sections"]["credits"]["data"]["balance"], 100)


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.workers = []

    def tearDown(self):
        for worker in self.workers:
            worker.stop()

    def worker(self, sender, **kwargs):
        worker = SyncWorker(sender=sender, min_interval=0, retry_base=0.02, **kwargs)
        self.workers.append(worker)
        return worker

    def test_url_policy(self):
        for allowed in (
            "https://edhq.io",
            "http://127.0.0.1:3000",
            "http://localhost:3000",
            "http://[::1]:3000",
        ):
            self.assertEqual(server_url(allowed), allowed)
        for denied in (
            "http://edhq.io",
            "https://user:pass@edhq.io",
            "https://edhq.io/api",
            "https://edhq.io?token=bad",
            "https://edhq.io#token",
            "file:///tmp",
            "https://",
        ):
            with self.subTest(url=denied), self.assertRaises(ValueError):
                server_url(denied)

    def test_worker_does_not_send_before_submit(self):
        sent = []
        worker = self.worker(lambda delivery: sent.append(delivery))
        self.assertEqual(sent, [])
        self.assertEqual(worker.get_status(), "Sync disabled")

    def test_queue_coalesces_to_latest_and_invalidates_account_changes(self):
        sent = []
        finished = threading.Event()
        worker = self.worker(
            lambda delivery: (sent.append(delivery.payload), finished.set(), (200, None))[-1]
        )
        with worker.condition:
            worker.submit("https://edhq.io", "fixture-token", {"old": 1})
            worker.submit("https://edhq.io", "fixture-token", {"latest": 2})
        self.assertTrue(finished.wait(1))
        self.assertEqual(sent, [{"latest": 2}])
        with worker.condition:
            worker.submit("https://edhq.io", "fixture-token", {"stale": 3})
            worker.configure()
        worker.stop()
        self.assertEqual(sent, [{"latest": 2}])

    def test_transient_failure_retries_without_replaying_events(self):
        sent = []
        finished = threading.Event()

        def sender(delivery):
            sent.append(copy.deepcopy(delivery.payload))
            if len(sent) == 1:
                raise OSError("synthetic failure")
            finished.set()
            return 200, None

        worker = self.worker(sender)
        worker.submit("https://edhq.io", "fixture-token", {"cumulative": 42})
        self.assertTrue(finished.wait(1))
        self.assertEqual(sent, [{"cumulative": 42}, {"cumulative": 42}])

    def test_permanent_rejection_pauses_until_reconfigured(self):
        finished = threading.Event()
        sent = []

        def sender(delivery):
            sent.append(delivery)
            finished.set()
            return 409, "commander_mismatch"

        worker = self.worker(sender)
        worker.submit("https://edhq.io", "fixture-token", {"commander": "A"})
        self.assertTrue(finished.wait(1))
        with worker.condition:
            # Wait for the result handler without relying on arbitrary sleeps.
            worker.condition.wait_for(lambda: worker.blocked, timeout=1)
        worker.submit("https://edhq.io", "fixture-token", {"commander": "B"})
        self.assertIn("mismatch", worker.get_status())
        self.assertEqual(len(sent), 1)
        worker.configure()
        self.assertFalse(worker.blocked)

    def test_shutdown_wait_is_bounded_during_network_request(self):
        started = threading.Event()
        release = threading.Event()

        def sender(delivery):
            started.set()
            release.wait(2)
            return 200, None

        worker = self.worker(sender)
        worker.submit("https://edhq.io", "fixture-token", {"fixture": True})
        self.assertTrue(started.wait(1))
        before = time.monotonic()
        worker.stop(timeout=0.01)
        self.assertLess(time.monotonic() - before, 0.2)
        release.set()

    def test_redirects_are_refused(self):
        import urllib.error
        import urllib.request

        with self.assertRaises(urllib.error.HTTPError) as caught:
            SafeRedirect().redirect_request(
                urllib.request.Request("https://edhq.io"),
                None,
                302,
                "Moved",
                {},
                "https://example.invalid",
            )
        caught.exception.close()

    def test_rate_limit_honors_retry_after(self):
        finished = threading.Event()

        def sender(delivery):
            finished.set()
            return 429, "rate_limited", 90

        worker = self.worker(sender)
        worker.submit("https://edhq.io", "fixture-token", {"fixture": True})
        self.assertTrue(finished.wait(1))
        with worker.condition:
            worker.condition.wait_for(lambda: worker.pending is not None, timeout=1)
            self.assertGreater(worker.next_send - time.monotonic(), 89)
        self.assertEqual(retry_delay("90"), 90)
        self.assertIsNone(retry_delay("not a date"))

    def test_request_only_serializes_explicit_snapshot(self):
        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, size):
                return b"{}"

        with patch("urllib.request.build_opener") as opener:
            opener.return_value.open.return_value = Response()
            payload = {
                "version": 1,
                "commander": {"name": "Fixture", "fid": "F100"},
                "sections": {},
            }
            self.assertEqual(
                post_snapshot(Delivery("https://edhq.io", "synthetic-test-token", payload, 0)),
                (200, None),
            )
            request = opener.return_value.open.call_args.args[0]
            self.assertEqual(request.full_url, "https://edhq.io/api/commander/sync")
            self.assertEqual(json.loads(request.data), payload)
            self.assertEqual(request.get_header("Authorization"), "Bearer synthetic-test-token")


if __name__ == "__main__":
    unittest.main()
