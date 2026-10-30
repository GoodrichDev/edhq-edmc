"""Opt-in, typed commander data from EDMC callbacks; no raw event forwarding."""

from __future__ import annotations

from collections.abc import Mapping
import copy
import hashlib
import json
import math
from datetime import timedelta

from .model import Collector as BaseCollector, RANKS, number, ship_id, text, timestamp

BASE_SECTIONS = {"ranks", "credits", "ships", "exploration"}
HISTORY = {"travel", "missions", "combat", "credit_history", "rank_history"}
OPTIONS = {
    "finances": "Assets and loans (use EDHQ Credits visibility)",
    "reputation": "Major/minor faction reputation",
    "powerplay": "Powerplay allegiance, rank and merits",
    "engineers": "Engineer unlocks and progress",
    "permits": "System permits",
    "inventory": "Materials, Odyssey locker and backpack contents",
    "cargo": "Cargo manifest",
    "stored_modules": "Stored modules and engineering",
    "loadouts": "Ship loadouts, engineering and values",
    "fleet_locations": "Owned ship locations and transfers (location information)",
    "equipment": "Suits, weapons and suit loadouts",
    "carrier": "Owned fleet carrier, services and finances",
    "location": "Current location (system, station and surface coordinates)",
    "statistics": "All numeric game statistics (including finances/activity)",
    "community_goals": "Community goals and contributions (destinations included)",
    "travel": "Travel history (visited systems, stations and planets)",
    "missions": "Mission history (destinations and rewards included)",
    "combat": "Combat history (including opponent names)",
    "credit_history": "Financial history (balances and transactions)",
    "rank_history": "Rank history",
}


def finite(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) and abs(value) <= 9e15 else None


def identity(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and 0 <= value <= 2**64 - 1:
        return str(value)
    if isinstance(value, str) and value.isascii() and value.isdigit() and len(value) <= 20:
        parsed = int(value)
        return str(parsed) if parsed <= 2**64 - 1 else None
    return None


def fields(source, mapping):
    """Map explicit source names to typed fields; unknown fields never survive."""
    result = {}
    if not isinstance(source, Mapping):
        return result
    for key, (source_key, kind) in mapping.items():
        value = source.get(source_key)
        if kind == "text":
            value = text(value, 256)
        elif kind == "id":
            value = identity(value)
        elif kind == "number":
            value = finite(value)
        elif kind == "bool":
            value = value if isinstance(value, bool) else None
        if value is not None:
            result[key] = value
    return result


def numeric_tree(source, depth=0, budget=None):
    if not isinstance(source, Mapping) or depth >= 3:
        return {}
    budget = [2048] if budget is None else budget
    result = {}
    for key, value in source.items():
        name = text(key, 100)
        if not name or budget[0] <= 0:
            continue
        if isinstance(value, Mapping):
            value = numeric_tree(value, depth + 1, budget)
            if not value:
                continue
        else:
            value = finite(value)
            if value is None:
                continue
        result[name] = value
        budget[0] -= 1
    return result


def engineering(source):
    result = fields(
        source,
        {
            "blueprint": ("BlueprintName", "text"),
            "level": ("Level", "number"),
            "quality": ("Quality", "number"),
            "experimental": ("ExperimentalEffect", "text"),
        },
    )
    if isinstance(source, Mapping) and isinstance(source.get("Modifiers"), list):
        modifiers = {}
        for item in source["Modifiers"][:128]:
            if not isinstance(item, Mapping):
                continue
            key = text(item.get("Label"), 100)
            value = finite(item.get("Value"))
            if key and value is not None:
                modifiers[key] = value
        if modifiers:
            result["modifiers"] = modifiers
    return result


LOCATION_FIELDS = {
    "system": ("StarSystem", "text"),
    "system_id": ("SystemAddress", "id"),
    "station": ("StationName", "text"),
    "market_id": ("MarketID", "id"),
    "body": ("Body", "text"),
    "body_id": ("BodyID", "id"),
    "latitude": ("Latitude", "number"),
    "longitude": ("Longitude", "number"),
    "docked": ("Docked", "bool"),
    "on_foot": ("OnFoot", "bool"),
}
TRAVEL_EVENTS = {
    "Location",
    "FSDJump",
    "CarrierJump",
    "Docked",
    "Undocked",
    "Touchdown",
    "Liftoff",
    "SupercruiseExit",
    "ApproachSettlement",
    "Disembark",
    "Embark",
    "DropshipDeploy",
}
COMBAT_EVENTS = {
    "Died",
    "Resurrect",
    "Interdicted",
    "Interdiction",
    "EscapeInterdiction",
    "PVPKill",
    "Bounty",
    "FactionKillBond",
    "CrimeVictim",
    "CommitCrime",
    "RedeemVoucher",
}
CREDIT_EVENTS = {
    "LoadGame",
    "Statistics",
    "MarketBuy",
    "MarketSell",
    "BuyTradeData",
    "BuyExplorationData",
    "SellExplorationData",
    "MultiSellExplorationData",
    "SellOrganicData",
    "ShipyardBuy",
    "ShipyardSell",
    "ShipyardTransfer",
    "ModuleBuy",
    "ModuleSell",
    "ModuleRetrieve",
    "ModuleStore",
    "BuySuit",
    "SellSuit",
    "BuyWeapon",
    "SellWeapon",
    "Repair",
    "RepairAll",
    "RefuelAll",
    "RefuelPartial",
    "RestockVehicle",
    "PayFines",
    "PayBounties",
    "PayLegacyFines",
    "MissionCompleted",
    "RedeemVoucher",
    "Resurrect",
    "CarrierBankTransfer",
    "CarrierBuy",
}


class Collector(BaseCollector):
    def __init__(self):
        super().__init__()
        self.options = set()
        self.history = {name: [] for name in HISTORY}
        self.last_credit_history = None
        self.collection_cache = {}

    def configure_options(self, enabled):
        self.options = set(enabled) & set(OPTIONS)
        for section in set(self.sections) - BASE_SECTIONS - self.options:
            self.sections.pop(section, None)
            self.collection_cache.pop(section, None)
        for section in HISTORY - self.options:
            self.history[section].clear()
        if "finances" not in self.options and "credits" in self.sections:
            self.sections["credits"]["data"].pop("assets", None)
            self.sections["credits"]["data"].pop("loan", None)
        if "location" not in self.options:
            for key in ("system", "system_id"):
                self.sections.get("carrier", {}).get("data", {}).pop(key, None)
        if not {"location", "fleet_locations"} & self.options:
            module_items = self.sections.get("stored_modules", {}).get("data", {}).get("items", [])
            cached_items = self.collection_cache.get("stored_modules", {}).values()
            for item in [*module_items, *cached_items]:
                for key in (
                    "system",
                    "system_id",
                    "station",
                    "market_id",
                    "transfer_time",
                    "transfer_price",
                ):
                    item.pop(key, None)

    def _next_stamp(self, name, source):
        # Reserve room for a split collection's ascending microsecond stamps.
        # History dates always remain the original journal timestamps.
        old = self.sections.get(name)
        if old:
            from datetime import datetime

            previous = datetime.fromisoformat(old["observed_at"].replace("Z", "+00:00"))
            source = max(source, previous + timedelta(milliseconds=2))
        return source.isoformat(timespec="microseconds").replace("+00:00", "Z")

    def _ranks(self, source, observed_at, capi=False):
        if not isinstance(source, Mapping):
            return
        values = {}
        for journal_name, key in RANKS.items():
            capi_key = {"Explore": "explore", "Soldier": "soldier"}.get(journal_name, key)
            raw = source.get(capi_key if capi else journal_name)
            sequence = isinstance(raw, (tuple, list))
            rank = number(raw[0] if sequence and raw else raw, 20, True)
            if rank is None:
                continue
            value = {"rank": rank}
            progress = number(raw[1], 100) if sequence and len(raw) > 1 else None
            if progress is not None:
                value["progress"] = progress
            else:
                old = self.sections.get("ranks", {}).get("data", {}).get(key, {})
                if old.get("rank") == rank and "progress" in old:
                    value["progress"] = old["progress"]
            values[key] = value
        self._section("ranks", values, observed_at)

    def _collection(self, name, items, at, complete=True, removed=None):
        if name not in self.options:
            return
        if self._source_time(name, at) is None:
            return
        identity = {
            "inventory": "id",
            "cargo": "id",
            "reputation": "id",
            "engineers": "id",
            "permits": "symbol",
            "stored_modules": "id",
            "loadouts": "game_id",
            "fleet_locations": "game_id",
            "equipment": "id",
            "community_goals": "id",
        }[name]
        previous = self.collection_cache.get(name, {})
        merged = [{**previous.get(item[identity], {}), **item} for item in items[:1000]]
        saved = {} if complete else dict(previous)
        saved.update({item[identity]: item for item in merged})
        for key in removed or []:
            saved.pop(key, None)
        self.collection_cache[name] = saved
        data = {"complete": bool(complete), "items": merged}
        # A truncated observation is partial and must never erase other records.
        if len(items) > 1000:
            data["complete"] = False
        if removed:
            data["removed"] = removed[:1000]
        self._section(name, data, at)

    def _event(self, section, entry, values):
        if section not in self.options:
            return
        at = timestamp(entry.get("timestamp"))
        if not at or not text(entry.get("event"), 100):
            return
        # Transient canonical event hashing survives replay even when EDMC's
        # accompanying state becomes more complete. No raw event is retained or
        # transmitted, and the fingerprint is scoped to this commander/family.
        item = {"at": at, "type": entry["event"], **values}
        try:
            identity = json.dumps(
                [self.fid, section, entry], sort_keys=True, separators=(",", ":"), allow_nan=False
            )
        except (TypeError, ValueError):
            return
        item["event_id"] = hashlib.sha256(identity.encode()).hexdigest()
        if not any(old["event_id"] == item["event_id"] for old in self.history[section]):
            if len(self.history[section]) >= 500:
                raise OverflowError("History outbox is full; restore sync before continuing")
            self.history[section].append(item)

    def acknowledge_history(self):
        for items in self.history.values():
            items.clear()

    def journal(self, commander, is_beta, entry, state):
        if not super().journal(commander, is_beta, entry, state):
            return False
        at, event = timestamp(entry.get("timestamp")), entry.get("event")
        if "finances" in self.options:
            extra = {}
            loan = number(state.get("Loan"))
            bank = entry.get("Bank_Account")
            wealth = bank.get("Current_Wealth") if isinstance(bank, Mapping) else None
            if loan is not None:
                extra["loan"] = loan
            if number(wealth) is not None:
                extra["assets"] = wealth
            if extra and "credits" in self.sections:
                self._section("credits", extra, at)
        self._progression(entry, state, at)
        self._inventory(entry, state, at)
        self._ships(entry, state, at)
        self._equipment(entry, state, at)
        self._carrier(entry, at)
        if "location" in self.options:
            # EDMC state supplies the current context; journal coordinates may
            # be absent on ordinary inventory/rank callbacks.
            context = {**entry}
            for key, state_key in (
                ("StarSystem", "SystemName"),
                ("SystemAddress", "SystemAddress"),
                ("StarPos", "StarPos"),
                ("StationName", "StationName"),
                ("Body", "Body"),
                ("BodyID", "BodyID"),
                ("Docked", "IsDocked"),
                ("OnFoot", "OnFoot"),
            ):
                if key not in context and state.get(state_key) is not None:
                    context[key] = state[state_key]
            value = fields(context, LOCATION_FIELDS)
            position = context.get("StarPos")
            if isinstance(position, (list, tuple)) and len(position) == 3:
                if all(finite(n) is not None for n in position):
                    value["position"] = list(position)
            self._section("location", value, at)
        stats = entry if event == "Statistics" else state.get("Statistics")
        if "statistics" in self.options and isinstance(stats, Mapping):
            categories = numeric_tree({k: v for k, v in stats.items() if isinstance(v, Mapping)})
            if categories:
                self._section("statistics", {"categories": categories}, at)
        if (
            "community_goals" in self.options
            and event == "CommunityGoal"
            and isinstance(entry.get("CurrentGoals"), list)
        ):
            goals = []
            for raw in entry["CurrentGoals"]:
                value = fields(
                    raw,
                    {
                        "id": ("CGID", "id"),
                        "title": ("Title", "text"),
                        "system": ("SystemName", "text"),
                        "station": ("MarketName", "text"),
                        "expiry": ("Expiry", "text"),
                        "contribution": ("PlayerContribution", "number"),
                        "tier": ("TierReached", "number"),
                        "percentile": ("PlayerPercentileBand", "number"),
                        "reward": ("Bonus", "number"),
                        "completed": ("IsComplete", "bool"),
                    },
                )
                if "id" in value:
                    goals.append(value)
            self._collection("community_goals", goals, at)
        self._histories(entry, state)
        return True

    def _progression(self, entry, state, at):
        if "reputation" in self.options and isinstance(state.get("Reputation"), Mapping):
            items = [
                {"id": "major:" + name.lower(), "name": name, "kind": "major", "reputation": value}
                for raw_name, raw in state["Reputation"].items()
                if (name := text(raw_name)) and (value := finite(raw)) is not None
            ]
            # Minor faction presence is a partial observation of reputation.
            if isinstance(entry.get("Factions"), list):
                for raw in entry["Factions"]:
                    name = text(raw.get("Name")) if isinstance(raw, Mapping) else None
                    value = finite(raw.get("MyReputation")) if isinstance(raw, Mapping) else None
                    if name and value is not None:
                        items.append(
                            {
                                "id": "minor:" + name.lower(),
                                "name": name,
                                "kind": "minor",
                                "reputation": value,
                            }
                        )
            self._collection("reputation", items, at, complete=False)
        if "powerplay" in self.options and isinstance(state.get("Powerplay"), Mapping):
            self._section(
                "powerplay",
                fields(
                    state["Powerplay"],
                    {
                        "power": ("Power", "text"),
                        "rank": ("Rank", "number"),
                        "merits": ("Merits", "number"),
                        "votes": ("Votes", "number"),
                        "time_pledged": ("TimePledged", "number"),
                    },
                ),
                at,
            )
        if "engineers" in self.options and isinstance(state.get("Engineers"), Mapping):
            items = []
            for raw_name, raw in state["Engineers"].items():
                name = text(raw_name)
                if not name:
                    continue
                value = {"id": name.lower(), "name": name}
                if isinstance(raw, (tuple, list)):
                    if raw and number(raw[0]) is not None:
                        value["rank"] = raw[0]
                    if len(raw) > 1 and number(raw[1]) is not None:
                        value["rank_progress"] = raw[1]
                elif text(raw):
                    value["progress"] = text(raw)
                items.append(value)
            self._collection("engineers", items, at)
        permits = entry.get("Permits") or entry.get("PermitsAwarded")
        if "permits" in self.options and isinstance(permits, list):
            self._collection(
                "permits",
                [{"symbol": name} for raw in permits if (name := text(raw))],
                at,
                complete=entry.get("event") == "Permit",
            )

    def _inventory(self, entry, state, at):
        if not {"inventory", "cargo"} & self.options:
            return
        categories = {
            key.lower(): state[key]
            for key in ("Raw", "Manufactured", "Encoded")
            if "inventory" in self.options and isinstance(state.get(key), Mapping)
        }
        for key in ("Component", "Item", "Consumable", "Data"):
            if "inventory" in self.options and isinstance(state.get(key), Mapping):
                categories["locker_" + key.lower()] = state[key]
            backpack = state.get("BackPack")
            if (
                "inventory" in self.options
                and isinstance(backpack, Mapping)
                and isinstance(backpack.get(key), Mapping)
            ):
                categories["backpack_" + key.lower()] = backpack[key]
        if categories:
            items = [
                {
                    "id": category + ":" + name,
                    "symbol": name,
                    "name": name,
                    "category": category,
                    "container": "backpack"
                    if category.startswith("backpack_")
                    else "locker"
                    if category.startswith("locker_")
                    else "commander",
                    "count": count,
                }
                for category, values in categories.items()
                for raw, n in values.items()
                if (name := text(raw)) and (count := number(n)) is not None
            ]
            self._collection(
                "inventory",
                items,
                at,
                complete=all(
                    isinstance(state.get(k), Mapping)
                    for k in (
                        "Raw",
                        "Manufactured",
                        "Encoded",
                        "Component",
                        "Item",
                        "Consumable",
                        "Data",
                    )
                )
                and isinstance(state.get("BackPack"), Mapping),
            )
        if "cargo" in self.options and isinstance(state.get("Cargo"), Mapping):
            manifest = state.get("CargoJSON")
            inventory = manifest.get("Inventory") if isinstance(manifest, Mapping) else None
            if isinstance(inventory, list):
                items = []
                for raw in inventory:
                    value = fields(
                        raw,
                        {
                            "symbol": ("Name", "text"),
                            "name": ("Name_Localised", "text"),
                            "count": ("Count", "number"),
                            "stolen": ("Stolen", "number"),
                            "mission_id": ("MissionID", "id"),
                        },
                    )
                    if "symbol" not in value or "count" not in value:
                        continue
                    value["id"] = (
                        value["symbol"]
                        + ":"
                        + value.get("mission_id", "")
                        + (":stolen" if value.get("stolen", 0) else ":legal")
                    )
                    items.append(value)
            else:
                items = [
                    {"id": name + "::aggregate", "symbol": name, "name": name, "count": count}
                    for raw, n in state["Cargo"].items()
                    if (name := text(raw)) and (count := number(n)) is not None
                ]
            self._collection("cargo", items, at)

    def _ships(self, entry, state, at):
        sale_field = {
            "ShipyardSell": "SellShipID",
            "ShipyardBuy": "SellShipID",
            "SellShipOnRebuy": "ShipID",
        }.get(entry.get("event"))
        sold = ship_id(entry.get(sale_field)) if sale_field else None
        source_time = self._source_time("ships", at) if sold else None
        if sold and source_time is not None:
            self.ships.pop(sold, None)
            self.sections["ships"] = {
                "observed_at": self._next_stamp("ships", source_time),
                "data": {"complete": False, "items": [], "removed": [sold]},
            }
            for section in ("loadouts", "fleet_locations"):
                self._collection(section, [], at, complete=False, removed=[sold])
        if (
            "loadouts" in self.options
            and ship_id(state.get("ShipID"))
            and ship_id(state.get("ShipID")) != sold
        ):
            ship = fields(
                state,
                {
                    "game_id": ("ShipID", "id"),
                    "ship_type": ("ShipType", "text"),
                    "name": ("ShipName", "text"),
                    "ident": ("ShipIdent", "text"),
                    "hull_value": ("HullValue", "number"),
                    "modules_value": ("ModulesValue", "number"),
                    "rebuy": ("Rebuy", "number"),
                    "max_jump_range": ("MaxJumpRange", "number"),
                    "fuel_capacity": ("FuelCapacity", "number"),
                    "cargo_capacity": ("CargoCapacity", "number"),
                },
            )
            modules = state.get("Modules")
            if isinstance(modules, Mapping):
                normalized = []
                for raw in list(modules.values())[:128]:
                    value = fields(
                        raw,
                        {
                            "slot": ("Slot", "text"),
                            "item": ("Item", "text"),
                            "enabled": ("On", "bool"),
                            "priority": ("Priority", "number"),
                            "health": ("Health", "number"),
                            "value": ("Value", "number"),
                        },
                    )
                    if value.get("slot") and value.get("item"):
                        if detail := engineering(raw.get("Engineering")):
                            value["engineering"] = detail
                        normalized.append(value)
                ship["modules"] = normalized
            self._collection("loadouts", [ship], at, complete=False)
        if (
            "stored_modules" in self.options
            and entry.get("event") == "StoredModules"
            and isinstance(entry.get("Items"), list)
        ):
            items = []
            for raw in entry["Items"]:
                value = fields(
                    raw,
                    {
                        "id": ("StorageSlot", "id"),
                        "item": ("Name", "text"),
                        "name": ("Name_Localised", "text"),
                        "value": ("BuyPrice", "number"),
                        "hot": ("Hot", "bool"),
                    },
                )
                if "id" in value and "item" in value:
                    detail = engineering(raw.get("Engineering"))
                    # StoredModules uses a blueprint symbol without full modifiers.
                    if isinstance(raw.get("Engineering"), str):
                        detail["blueprint"] = text(raw["Engineering"])
                    if detail:
                        value["engineering"] = detail
                    if "fleet_locations" in self.options or "location" in self.options:
                        value.update(
                            fields(
                                raw,
                                {
                                    "system": ("StarSystem", "text"),
                                    "market_id": ("MarketID", "id"),
                                    "transfer_time": ("TransferTime", "number"),
                                    "transfer_price": ("TransferCost", "number"),
                                },
                            )
                        )
                    items.append(value)
            self._collection("stored_modules", items, at)
        if "fleet_locations" in self.options:
            items = []
            if entry.get("event") == "StoredShips":
                for raw in (entry.get("ShipsHere") or []) + (entry.get("ShipsRemote") or []):
                    value = fields(
                        raw,
                        {
                            "game_id": ("ShipID", "id"),
                            "system": ("StarSystem", "text"),
                            "market_id": ("ShipMarketID", "id"),
                            "in_transit": ("InTransit", "bool"),
                            "transfer_time": ("TransferTime", "number"),
                            "transfer_price": ("TransferPrice", "number"),
                        },
                    )
                    if "game_id" in value:
                        items.append(value)
            if entry.get("event") == "ShipyardTransfer":
                value = fields(
                    entry,
                    {
                        "game_id": ("ShipID", "id"),
                        "system": ("System", "text"),
                        "market_id": ("ShipMarketID", "id"),
                        "transfer_time": ("TransferTime", "number"),
                        "transfer_price": ("TransferPrice", "number"),
                    },
                )
                if "game_id" in value:
                    value["in_transit"] = True
                    items.append(value)
            if (
                ship_id(state.get("ShipID"))
                and ship_id(state.get("ShipID")) != sold
                and text(state.get("SystemName"))
            ):
                value = {
                    "game_id": ship_id(state["ShipID"]),
                    "system": text(state["SystemName"]),
                    "in_transit": False,
                }
                if text(state.get("StationName")):
                    value["station"] = text(state["StationName"])
                items.append(value)
            if items:
                self._collection("fleet_locations", items, at, complete=False)

    def _equipment(self, entry, state, at):
        if "equipment" not in self.options:
            return
        items = []
        suits = state.get("Suits")
        if isinstance(suits, Mapping):
            for identity, raw in suits.items():
                if not isinstance(raw, Mapping) or not ship_id(identity):
                    continue
                value = {"id": "suit:" + ship_id(identity), "kind": "suit"}
                value.update(
                    fields(raw, {"name": ("name", "text"), "grade": ("suitClass", "number")})
                )
                mods = raw.get("mods")
                if isinstance(mods, list):
                    value["mods"] = [s for raw_mod in mods[:32] if (s := text(raw_mod))]
                items.append(value)
        if isinstance(state.get("SuitLoadouts"), Mapping):
            for identity, raw in state["SuitLoadouts"].items():
                if not isinstance(raw, Mapping) or not ship_id(identity):
                    continue
                # EDMC exposes CAPI-oriented suit slots (0..9), whereas journal
                # LoadoutID is the game's slot number plus 4293000000.
                game_loadout = str(int(identity) + 4_293_000_000)
                value = {
                    "id": "loadout:" + game_loadout,
                    "kind": "suit_loadout",
                    "loadout_id": game_loadout,
                }
                value.update(fields(raw, {"name": ("name", "text")}))
                slots = []
                if isinstance(raw.get("slots"), Mapping):
                    for key, slot in raw["slots"].items():
                        if not isinstance(slot, Mapping):
                            continue
                        weapon = fields(
                            slot,
                            {
                                "id": ("weaponrackId", "id"),
                                "name": ("name", "text"),
                                "grade": ("class", "number"),
                            },
                        )
                        if "id" in weapon:
                            slots.append(
                                {
                                    "slot": text(key),
                                    "id": weapon["id"],
                                    "name": weapon.get("name", ""),
                                }
                            )
                            weapon["id"] = "weapon:" + weapon["id"]
                            weapon["kind"] = "weapon"
                            if isinstance(slot.get("mods"), list):
                                weapon["mods"] = [s for m in slot["mods"][:32] if (s := text(m))]
                            items.append(weapon)
                value["slots"] = slots
                items.append(value)
        # Event-level records cover weapons not currently assigned to a loadout.
        if entry.get("event") in {"BuyWeapon", "UpgradeWeapon"}:
            value = fields(
                entry,
                {
                    "id": ("SuitModuleID", "id"),
                    "name": ("Name", "text"),
                    "grade": ("Class", "number"),
                },
            )
            if "id" in value:
                value["id"] = "weapon:" + value["id"]
                value["kind"] = "weapon"
                if isinstance(entry.get("WeaponMods"), list):
                    value["mods"] = [s for m in entry["WeaponMods"][:32] if (s := text(m))]
                items.append(value)
        removed = []
        if entry.get("event") in {"SellSuit", "SellWeapon", "DeleteSuitLoadout"}:
            key, prefix = {
                "SellSuit": ("SuitID", "suit:"),
                "SellWeapon": ("SuitModuleID", "weapon:"),
                "DeleteSuitLoadout": ("LoadoutID", "loadout:"),
            }[entry["event"]]
            if ship_id(entry.get(key)):
                removed.append(prefix + ship_id(entry[key]))
        if items or removed:
            # Multiple loadouts can contain the same weapon.
            unique = {item["id"]: item for item in items}
            self._collection(
                "equipment", list(unique.values()), at, complete=False, removed=removed
            )

    def _carrier(self, entry, at):
        if "carrier" not in self.options or entry.get("event") not in {
            "CarrierStats",
            "CarrierFinance",
        }:
            return
        value = fields(
            entry,
            {
                "id": ("CarrierID", "id"),
                "callsign": ("Callsign", "text"),
                "name": ("Name", "text"),
                "fuel": ("FuelLevel", "number"),
                "jump_range": ("JumpRangeCurr", "number"),
                "crew_count": ("CrewCount", "number"),
                "docking_access": ("DockingAccess", "text"),
                "allow_notorious": ("AllowNotorious", "bool"),
            },
        )
        value.update(
            fields(
                entry.get("Finance"),
                {
                    "balance": ("CarrierBalance", "number"),
                    "reserve_balance": ("ReserveBalance", "number"),
                    "available_balance": ("AvailableBalance", "number"),
                },
            )
        )
        value.update(fields(entry.get("SpaceUsage"), {"space_free": ("FreeSpace", "number")}))
        space = entry.get("SpaceUsage")
        if isinstance(space, Mapping):
            total, free = number(space.get("TotalCapacity")), number(space.get("FreeSpace"))
            if total is not None and free is not None and total >= free:
                value["space_used"] = total - free
        if "location" in self.options:
            value.update(
                fields(
                    entry, {"system": ("StarSystem", "text"), "system_id": ("SystemAddress", "id")}
                )
            )
        crew = entry.get("Crew")
        if isinstance(crew, list):
            value["services"] = [
                s
                for item in crew
                if isinstance(item, Mapping) and (s := text(item.get("CrewRole")))
            ]
        self._section("carrier", value, at)

    def _histories(self, entry, state):
        event = entry.get("event")
        if "travel" in self.options and event in TRAVEL_EVENTS:
            value = fields(
                entry,
                {
                    **{
                        k: v
                        for k, v in LOCATION_FIELDS.items()
                        if k in {"system", "system_id", "station", "market_id", "body"}
                    },
                    "distance_ly": ("JumpDist", "number"),
                    "fuel_used": ("FuelUsed", "number"),
                },
            )
            value.update(
                fields(state, {"ship_id": ("ShipID", "id"), "ship_type": ("ShipType", "text")})
            )
            position = entry.get("StarPos")
            if (
                isinstance(position, (list, tuple))
                and len(position) == 3
                and all(finite(n) is not None for n in position)
            ):
                value["position"] = list(position)
            self._event("travel", entry, value)
        if "missions" in self.options and isinstance(event, str) and event.startswith("Mission"):
            self._event(
                "missions",
                entry,
                fields(
                    entry,
                    {
                        "mission_id": ("MissionID", "id"),
                        "name": ("Name", "text"),
                        "faction": ("Faction", "text"),
                        "destination_system": ("DestinationSystem", "text"),
                        "destination_station": ("DestinationStation", "text"),
                        "reward": ("Reward", "number"),
                        "commodity": ("Commodity", "text"),
                        "count": ("Count", "number"),
                        "expiry": ("Expiry", "text"),
                        "influence": ("Influence", "text"),
                        "reputation": ("Reputation", "text"),
                    },
                ),
            )
        if "combat" in self.options and event in COMBAT_EVENTS:
            value = fields(
                entry,
                {
                    "reward": ("TotalReward", "number"),
                    "victim_ship": ("VictimShip", "text"),
                    "faction": ("Faction", "text"),
                    "combat_rank": ("CombatRank", "number"),
                    "crime_type": ("CrimeType", "text"),
                    "count": ("Count", "number"),
                    "is_player": ("IsPlayer", "bool"),
                },
            )
            opponent_key = {
                "Interdicted": "Interdictor",
                "Interdiction": "Interdicted",
                "PVPKill": "Victim",
                "Died": "KillerName",
            }.get(event)
            if opponent_key and text(entry.get(opponent_key)):
                value["opponent"] = text(entry[opponent_key])
            if event == "Died" and isinstance(entry.get("Killers"), list):
                value["wing_opponents"] = [
                    name
                    for raw in entry["Killers"][:32]
                    if isinstance(raw, Mapping) and (name := text(raw.get("Name")))
                ]
            self._event("combat", entry, value)
        balance = number(state.get("Credits"))
        previous = self.last_credit_history
        original_time = timestamp(entry.get("timestamp"))
        meaningful_credit = False
        if balance is not None and "credit_history" in self.options:
            from datetime import datetime

            at = datetime.fromisoformat(original_time.replace("Z", "+00:00"))
            meaningful_credit = (
                previous is None
                or event in {"LoadGame", "Shutdown"}
                or (at - previous[1]).total_seconds() >= 3600
                or abs(balance - previous[0]) >= max(10_000, previous[0] * 0.01)
            )
        if meaningful_credit:
            value = fields(
                entry,
                {
                    "amount": ("Amount", "number"),
                    "cost": ("Cost", "number"),
                    "reward": ("Reward", "number"),
                    "commodity": ("Type", "text"),
                    "count": ("Count", "number"),
                    "ship_id": ("ShipID", "id"),
                    "item": ("BuyItem", "text"),
                },
            )
            value.update(
                fields(state, {"balance": ("Credits", "number"), "loan": ("Loan", "number")})
            )
            bank = entry.get("Bank_Account")
            wealth = bank.get("Current_Wealth") if isinstance(bank, Mapping) else None
            if finite(wealth) is not None:
                value["assets"] = wealth
            self._event("credit_history", entry, value)
            self.last_credit_history = (balance, at)
        if "rank_history" in self.options and event in {"Rank", "Progress", "Promotion"}:
            ranks = state.get("Rank")
            if isinstance(ranks, Mapping):
                value = {}
                for source, key in RANKS.items():
                    raw = ranks.get(source)
                    rank = raw[0] if isinstance(raw, (list, tuple)) and raw else raw
                    if number(rank, 20, True) is not None:
                        value[key] = rank
                self._event("rank_history", entry, value)

    def snapshot(self):
        result = super().snapshot()
        if result is None:
            return None
        result["version"] = 2
        for section, items in self.history.items():
            if section in self.options and items:
                result["sections"][section] = {
                    "observed_at": items[-1]["at"],
                    "data": {"items": copy.deepcopy(items)},
                }
        return result
