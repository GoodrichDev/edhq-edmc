"""Synthetic v2 privacy, collection and durable transport qualification."""

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
if "edhq" not in sys.modules:
    spec = importlib.util.spec_from_file_location(
        "edhq", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["edhq"] = module
    spec.loader.exec_module(module)

from edhq.model_v2 import BASE_SECTIONS, OPTIONS, Collector
from edhq.sync_v2 import Outbox, SyncWorker

AT = "2026-10-29T01:00:00Z"


def state(**extra):
    return {
        "FID": "F100",
        "GameVersion": "4.4",
        "Credits": 1_000_000,
        "Loan": 123,
        "Rank": {"Combat": (3, 42)},
        "ShipID": 7,
        "ShipType": "asp",
        "ShipName": "Fixture",
        "ShipIdent": "TEST",
        **extra,
    }


def collect(options=(), entry=None, values=None):
    collector = Collector()
    collector.select("Fixture Commander")
    collector.configure_options(options)
    collector.journal(
        "Fixture Commander", False, entry or {"timestamp": AT, "event": "Rank"}, values or state()
    )
    return collector


def fixture_payload():
    collector = collect(
        OPTIONS,
        {"timestamp": AT, "event": "Statistics", "Bank_Account": {"Current_Wealth": 2_000_000}},
        state(
            Reputation={"Federation": -12.5},
            Powerplay={"Power": "Example", "Rank": 4, "Merits": 56},
            Engineers={"Fixture Engineer": (3, 50)},
            Raw={"iron": 3},
            Manufactured={},
            Encoded={},
            Component={"chemical": 1},
            Item={},
            Consumable={},
            Data={},
            BackPack={"Component": {"chemical": 2}, "Item": {}, "Consumable": {}, "Data": {}},
            Cargo={"gold": 4},
            CargoJSON={"Inventory": [{"Name": "gold", "Count": 4, "Stolen": 1, "MissionID": 25}]},
            HullValue=1000,
            ModulesValue=2000,
            Rebuy=150,
            Modules={
                "Slot01": {
                    "Slot": "Slot01",
                    "Item": "fixture_module",
                    "On": True,
                    "Engineering": {
                        "BlueprintName": "fixture",
                        "Level": 5,
                        "Modifiers": [{"Label": "Mass", "Value": -2}],
                    },
                }
            },
            SystemName="Sol",
            SystemAddress=10477373803,
            StarPos=[0, -1, 2],
            Suits={"50": {"name": "utilitysuit_class1", "mods": ["fixture_mod"]}},
            SuitLoadouts={
                "1": {
                    "name": "Fixture loadout",
                    "slots": {
                        "PrimaryWeapon1": {"weaponrackId": 60, "name": "fixture_weapon", "class": 2}
                    },
                }
            },
            Statistics={"Combat": {"Bounties_Claimed": 4}, "PrivateText": "must-not-send"},
        ),
    )
    for entry in [
        {
            "event": "MissionCompleted",
            "MissionID": 20,
            "Name": "Fixture mission",
            "Reward": 12000,
            "PermitsAwarded": ["Sol"],
            "DestinationSystem": "Sol",
        },
        {
            "event": "StoredModules",
            "Items": [{"StorageSlot": 1, "Name": "fixture_module", "BuyPrice": 10}],
        },
        {
            "event": "FSDJump",
            "StarSystem": "Sol",
            "SystemAddress": 10477373803,
            "StarPos": [0, -1, 2],
            "JumpDist": 25,
            "FuelUsed": 2,
        },
        {
            "event": "Interdicted",
            "Interdictor": "Fixture Opponent",
            "IsPlayer": True,
            "CombatRank": 3,
        },
        {
            "event": "CarrierStats",
            "CarrierID": 10,
            "Callsign": "ABC-123",
            "Name": "Fixture Carrier",
            "Finance": {"CarrierBalance": 1000},
            "SpaceUsage": {"TotalCapacity": 25000, "FreeSpace": 24000},
        },
        {
            "event": "CommunityGoal",
            "CurrentGoals": [
                {
                    "CGID": 1,
                    "Title": "Fixture goal",
                    "SystemName": "Sol",
                    "PlayerContribution": 100,
                    "IsComplete": False,
                }
            ],
        },
        {"event": "Rank"},
    ]:
        collector.journal("Fixture Commander", False, {"timestamp": AT, **entry}, state())
    return collector.snapshot()


class CaptureTests(unittest.TestCase):
    def test_default_scope_and_actual_rank_progress(self):
        result = collect().snapshot()
        self.assertLessEqual(set(result["sections"]), BASE_SECTIONS)
        self.assertEqual(result["sections"]["ranks"]["data"]["combat"], {"rank": 3, "progress": 42})
        self.assertEqual(result["sections"]["credits"]["data"], {"balance": 1_000_000})

    def test_all_families_and_no_raw_private_fields(self):
        result = fixture_payload()
        expected = BASE_SECTIONS | (set(OPTIONS) - {"finances"})
        self.assertEqual(set(result["sections"]), expected - {"exploration"})
        serialized = json.dumps(result)
        for forbidden in ("must-not-send", "uploaderID", "Friends", "FrontierToken", "raw_journal"):
            self.assertNotIn(forbidden, serialized)
        credits = result["sections"]["credits"]["data"]
        self.assertEqual(credits["assets"], 2_000_000)
        self.assertEqual(credits["loan"], 123)
        inventory = result["sections"]["inventory"]["data"]["items"]
        self.assertEqual(len({item["id"] for item in inventory}), len(inventory))
        chemicals = [item for item in inventory if item["symbol"] == "chemical"]
        self.assertEqual({item["container"] for item in chemicals}, {"locker", "backpack"})
        self.assertEqual(result["sections"]["carrier"]["data"]["space_used"], 1000)

    def test_rank_progress_does_not_retimestamp_unchanged_callbacks(self):
        collector = collect()
        original = collector.snapshot()["sections"]["ranks"]
        collector.journal("Fixture Commander", False, {"timestamp": AT, "event": "Music"}, state())
        self.assertEqual(original, collector.snapshot()["sections"]["ranks"])

    def test_history_replay_is_idempotent_and_original_date_is_preserved(self):
        entry = {"timestamp": AT, "event": "FSDJump", "StarSystem": "Fixture", "JumpDist": 12}
        collector = collect({"travel"}, entry)
        collector.journal("Fixture Commander", False, entry, state())
        history = collector.snapshot()["sections"]["travel"]["data"]["items"]
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["at"], "2026-10-29T01:00:00.000000Z")
        self.assertEqual(len(history[0]["event_id"]), 64)

    def test_small_credit_changes_do_not_spam_and_milestone_is_authoritative(self):
        collector = collect(
            {"credit_history"},
            {"timestamp": AT, "event": "LoadGame", "GameMode": "Open", "Ship": "asp"},
        )
        collector.acknowledge_history()
        collector.journal(
            "Fixture Commander",
            False,
            {"timestamp": AT, "event": "MarketBuy", "Cost": 100},
            state(Credits=999900),
        )
        self.assertEqual(collector.history["credit_history"], [])
        collector.journal(
            "Fixture Commander",
            False,
            {"timestamp": AT, "event": "MarketBuy", "Cost": 10000},
            state(Credits=989900),
        )
        self.assertEqual(collector.history["credit_history"][0]["balance"], 989900)

    def test_disabling_options_drops_unsent_extension_state_and_switch_resets(self):
        collector = collect(
            {"location", "travel"}, {"timestamp": AT, "event": "FSDJump", "StarSystem": "Secret"}
        )
        collector.configure_options(set())
        self.assertNotIn("location", collector.snapshot()["sections"])
        self.assertNotIn("travel", collector.snapshot()["sections"])
        collector.select("Other Commander")
        self.assertFalse(collector.options)
        self.assertIsNone(collector.snapshot())

    def test_legacy_and_multicrew_never_capture_extensions(self):
        for extra in ({"GameVersion": "3.8"}, {"Captain": "Other"}):
            collector = collect({"location", "travel"}, values=state(**extra))
            self.assertIsNone(collector.snapshot())

    def test_exact_unsigned_system_identity_and_opponent_consent(self):
        entry = {
            "event": "FSDJump",
            "timestamp": AT,
            "SystemAddress": 18446744073709551615,
            "StarSystem": "Fixture",
        }
        result = collect({"travel"}, entry).snapshot()
        self.assertEqual(
            result["sections"]["travel"]["data"]["items"][0]["system_id"], "18446744073709551615"
        )
        combat = {
            "event": "Interdicted",
            "timestamp": AT,
            "Interdictor": "Fixture Opponent",
            "IsPlayer": True,
        }
        self.assertNotIn("Fixture Opponent", json.dumps(collect((), combat).snapshot()))
        allowed = collect({"combat"}, combat).snapshot()
        self.assertEqual(
            allowed["sections"]["combat"]["data"]["items"][0]["opponent"], "Fixture Opponent"
        )

    def test_sales_emit_explicit_tombstones_without_inventing_complete_fleets(self):
        collector = collect({"loadouts", "fleet_locations"})
        collector.journal(
            "Fixture Commander",
            False,
            {"event": "ShipyardSell", "timestamp": AT, "SellShipID": 7},
            state(),
        )
        for section in ("ships", "loadouts", "fleet_locations"):
            data = collector.snapshot()["sections"][section]["data"]
            self.assertFalse(data["complete"])
            self.assertIn("7", data["removed"])

    def test_history_identity_survives_more_complete_accompanying_state(self):
        entry = {"event": "FSDJump", "timestamp": AT, "StarSystem": "Fixture"}
        collector = collect({"travel"}, entry, state(ShipID=None, ShipType=None))
        collector.journal("Fixture Commander", False, entry, state())
        self.assertEqual(len(collector.history["travel"]), 1)


class DurableTests(unittest.TestCase):
    def test_history_survives_restart_and_contains_no_token(self):
        payload = fixture_payload()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "outbox.sqlite3"
            first = Outbox(path)
            first.add("hashed-target", payload)
            first.add("hashed-target", payload)
            count = first.db.execute(
                "SELECT count(*) FROM pending WHERE section IN "
                "('travel','missions','combat','credit_history','rank_history')"
            ).fetchone()[0]
            self.assertEqual(count, 5)
            first.db.close()
            second = Outbox(path)
            selected, rows = second.prepare("hashed-target", payload["commander"], 2)
            self.assertIn("travel", selected["sections"])
            self.assertIsNone(second.prepare("different-target", payload["commander"], 2)[0])
            second.acknowledge(rows)
            self.assertEqual(second.db.execute("SELECT count(*) FROM pending").fetchone()[0], 0)
            second.db.close()
            self.assertNotIn(b"synthetic-bearer-token", path.read_bytes())

    def test_partial_snapshots_merge_while_offline_and_splitting_resumes(self):
        box = Outbox(":memory:")
        payload = {
            "version": 2,
            "commander": {"name": "Fixture", "fid": "F1"},
            "sections": {
                "loadouts": {
                    "observed_at": AT,
                    "data": {
                        "complete": False,
                        "items": [
                            {"game_id": str(i), "ship_type": "fixture", "name": "x" * 100}
                            for i in range(230)
                        ],
                    },
                }
            },
        }
        box.add("target", payload)
        sent = []
        dates = []
        while True:
            next_payload, rows = box.prepare("target", payload["commander"], 2, limit=9000)
            if not next_payload:
                break
            data = next_payload["sections"]["loadouts"]
            sent.extend(item["game_id"] for item in data["data"]["items"])
            dates.append(data["observed_at"])
            self.assertLessEqual(
                len(json.dumps(next_payload, separators=(",", ":")).encode()), 9000
            )
            # An unchanged callback during an in-flight batch must not reset its cursor.
            box.add("target", payload)
            box.acknowledge(rows)
        self.assertEqual(sent, [str(i) for i in range(230)])
        self.assertEqual(dates, sorted(set(dates)))
        box.db.close()

    def test_acknowledgement_does_not_delete_replaced_snapshot(self):
        box = Outbox(":memory:")
        payload = {
            "version": 2,
            "commander": {"name": "Fixture", "fid": "F1"},
            "sections": {"credits": {"observed_at": AT, "data": {"balance": 10}}},
        }
        box.add("target", payload)
        _, rows = box.prepare("target", payload["commander"], 2)
        payload["sections"]["credits"]["data"]["balance"] = 20
        box.add("target", payload)
        box.acknowledge(rows)
        self.assertEqual(
            box.prepare("target", payload["commander"], 2)[0]["sections"]["credits"]["data"][
                "balance"
            ],
            20,
        )
        box.db.close()

    def test_v1_fallback_retains_extra_history_for_later_upgrade(self):
        box = Outbox(":memory:")
        payload = fixture_payload()
        box.add("target", payload)
        selected, rows = box.prepare("target", payload["commander"], 1)
        self.assertLessEqual(set(selected["sections"]), BASE_SECTIONS)
        self.assertNotIn("loan", selected["sections"]["credits"]["data"])
        box.acknowledge(rows)
        self.assertIn("travel", box.prepare("target", payload["commander"], 2)[0]["sections"])
        box.db.close()

    def test_disabling_history_purges_only_that_channel(self):
        box = Outbox(":memory:")
        payload = fixture_payload()
        box.add("a", payload)
        box.add("b", payload)
        box.restrict("a", BASE_SECTIONS)
        self.assertEqual(
            box.db.execute(
                "SELECT count(*) FROM pending WHERE target='a' AND section='travel'"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            box.db.execute(
                "SELECT count(*) FROM pending WHERE target='b' AND section='travel'"
            ).fetchone()[0],
            1,
        )
        box.db.close()

    def test_disabling_subfield_consent_redacts_already_queued_values(self):
        box = Outbox(":memory:")
        payload = {
            "version": 2,
            "commander": {"name": "Fixture", "fid": "F1"},
            "sections": {
                "credits": {"observed_at": AT, "data": {"balance": 10, "assets": 20, "loan": 3}},
                "carrier": {"observed_at": AT, "data": {"id": "1", "system": "Private system"}},
                "stored_modules": {
                    "observed_at": AT,
                    "data": {
                        "complete": False,
                        "items": [{"id": "1", "item": "module", "system": "Private system"}],
                    },
                },
            },
        }
        box.add("target", payload)
        box.restrict("target", BASE_SECTIONS | {"carrier", "stored_modules"})
        result = box.prepare("target", payload["commander"], 2)[0]
        self.assertNotIn("Private system", json.dumps(result))
        self.assertEqual(result["sections"]["credits"]["data"], {"balance": 10})
        box.db.close()

    def test_worker_background_send_and_shutdown(self):
        arrived = threading.Event()
        calls = []

        def sender(delivery):
            calls.append(delivery)
            arrived.set()
            return 200, None

        with tempfile.TemporaryDirectory() as directory:
            worker = SyncWorker(
                Path(directory) / "outbox.sqlite3",
                sender=sender,
                discover=lambda _: {"sync_versions": [1, 2]},
                min_interval=0,
            )
            self.assertFalse(arrived.wait(0.01))
            worker.submit(
                "https://example.invalid",
                "synthetic-bearer-token",
                fixture_payload(),
                BASE_SECTIONS | set(OPTIONS),
            )
            self.assertTrue(arrived.wait(1))
            worker.stop()
            self.assertFalse(worker.thread.is_alive())
            self.assertEqual(calls[0].payload["version"], 2)
            self.assertNotIn("synthetic-bearer-token", json.dumps(calls[0].payload))

    def test_offline_history_is_not_sent_after_option_is_disabled(self):
        failed, delivered = threading.Event(), threading.Event()
        online = [False]
        calls = []

        def sender(delivery):
            calls.append(delivery.payload)
            if not online[0]:
                failed.set()
                return 503, None
            delivered.set()
            return 200, None

        with tempfile.TemporaryDirectory() as directory:
            worker = SyncWorker(
                Path(directory) / "outbox.sqlite3",
                sender=sender,
                discover=lambda _: {"sync_versions": [1, 2]},
                min_interval=0,
            )
            payload = fixture_payload()
            worker.submit(
                "https://example.invalid",
                "synthetic-bearer-token",
                payload,
                BASE_SECTIONS | set(OPTIONS),
            )
            self.assertTrue(failed.wait(1))
            worker.configure()
            basic = {
                **payload,
                "sections": {k: v for k, v in payload["sections"].items() if k in BASE_SECTIONS},
            }
            online[0] = True
            worker.submit("https://example.invalid", "synthetic-bearer-token", basic, BASE_SECTIONS)
            self.assertTrue(delivered.wait(1))
            worker.stop()
            self.assertNotIn("travel", calls[-1]["sections"])
            self.assertLessEqual(set(calls[-1]["sections"]), BASE_SECTIONS)


if __name__ == "__main__":
    unittest.main()
