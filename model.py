"""Allowlisted EDMC snapshots. This module never reads journals or credentials."""

from __future__ import annotations

import copy
import math
import re
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone

RANKS = {
    "Combat": "combat",
    "Trade": "trade",
    "Explore": "exploration",
    "Soldier": "mercenary",
    "Exobiologist": "exobiologist",
    "CQC": "cqc",
    "Federation": "federation",
    "Empire": "empire",
}
EXPLORATION = {
    "Systems_Visited": "systems_visited",
    "Exploration_Profits": "exploration_profits",
    "Planets_Scanned_To_Level_2": "planets_scanned_level_2",
    "Planets_Scanned_To_Level_3": "planets_scanned_level_3",
    "Stars_Scanned_To_Level_2": "stars_scanned_level_2",
    "Stars_Scanned_To_Level_3": "stars_scanned_level_3",
    "Planets_Scanned": "planets_scanned",
    "Level_2_Detailed_Scans": "level_2_detailed_scans",
    "Level_3_Detailed_Scans": "level_3_detailed_scans",
    "Efficient_Scans": "efficient_scans",
    "Highest_Payout": "highest_payout",
    "Total_Hyperspace_Distance": "hyperspace_distance_ly",
    "Hyperspace_Total_Distance": "hyperspace_distance_ly",
    "Total_Hyperspace_Jumps": "hyperspace_jumps",
    "Greatest_Distance_From_Start": "greatest_distance_from_start_ly",
    "Time_Played": "time_played",
    "Total_Earnings": "total_earnings",
}
EXOBIOLOGY = {
    "OnFoot_Organics_Logged": "onfoot_organics_logged",
    "OnFoot_Organics_Analysed": "onfoot_organics_analysed",
    "OnFoot_Organic_Data_Profits": "organic_data_profits",
}


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def timestamp(value):
    """Return a normalized timestamp without inventing an event's age."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        return (
            parsed.astimezone(timezone.utc)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )
    except ValueError:
        return None


def text(value, limit=128):
    if not isinstance(value, str):
        return None
    value = "".join(char for char in value.strip() if char.isprintable())
    return value.encode("utf-8")[:limit].decode("utf-8", errors="ignore") or None


def number(value, maximum=9e15, integer=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0 or value > maximum:
        return None
    if integer and int(value) != value:
        return None
    return int(value) if integer else value


def ship_id(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and 0 <= value <= 2**63 - 1:
        return str(value)
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,19}", value):
        return str(int(value))
    return None


def normalized_ship(source, current=False, capi=False, fallback_id=None):
    if not isinstance(source, Mapping):
        return None
    game_id = ship_id(source.get("id") if capi else source.get("ShipID"))
    game_id = game_id or ship_id(fallback_id)
    ship_type = text(source.get("name") if capi else source.get("ShipType"), 64)
    if game_id is None or ship_type is None:
        return None
    # SRVs, fighters and on-foot suits are not independently owned fleet ships.
    if (
        ship_type.casefold()
        in {"testbuggy", "fighter", "empire_fighter", "federation_fighter", "independent_fighter"}
        or "suit" in ship_type.casefold()
    ):
        return None
    result = {"game_id": game_id, "ship_type": ship_type, "current": bool(current)}
    name = source.get("shipName") if capi else source.get("Name")
    ident = source.get("shipID") if capi else source.get("ShipIdent")
    if isinstance(name, str):
        result["name"] = text(name, 100) or ""
    if isinstance(ident, str):
        result["ident"] = text(ident, 32) or ""
    return result


class Collector:
    """State for exactly one active commander; incomplete data stays omitted."""

    def __init__(self):
        self.commander = None
        self.fid = None
        self.sections = {}
        self.section_sources = {}
        self.ships = {}
        self.fleet_complete = False
        self.blocked = False
        self.identity_error = False

    def select(self, commander):
        commander = text(commander, 100)
        if not commander:
            return False
        if not self.commander or self.commander.casefold() != commander.casefold():
            self.__init__()
            self.commander = commander
        return True

    def _identity(self, commander, fid):
        if not self.select(commander):
            return False
        fid = text(fid, 64)
        if not fid or not re.fullmatch(r"F[0-9]{1,20}", fid):
            return False
        if self.fid and self.fid != fid:
            self.identity_error = True
            self.blocked = True
            return False
        self.fid = fid
        return True

    def _section(self, name, values, observed_at, **extra):
        if not values:
            return
        source = self._source_time(name, observed_at)
        if source is None:
            return
        old = self.sections.get(name)
        if old and isinstance(old["data"], dict) and isinstance(values, dict):
            values = {**old["data"], **values}
        # Repeated state on unrelated events must not manufacture freshness.
        if old and old["data"] == values and all(old.get(k) == v for k, v in extra.items()):
            return
        self.sections[name] = {
            "observed_at": self._next_stamp(name, source),
            "data": copy.deepcopy(values),
            **extra,
        }

    def _source_time(self, name, observed_at):
        source = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        previous = self.section_sources.get(name)
        if previous is not None and source < previous:
            return None
        # Keep freshness even when a newer authoritative observation has the
        # same value, so an older callback cannot roll it back later.
        self.section_sources[name] = source
        return source

    def _next_stamp(self, name, source):
        old = self.sections.get(name)
        if old:
            previous = datetime.fromisoformat(old["observed_at"].replace("Z", "+00:00"))
            if source <= previous:
                # Journal timestamps have only second precision. Sequence
                # distinct observations without changing the source age guard.
                source = previous + timedelta(microseconds=1)
        return source.isoformat(timespec="microseconds").replace("+00:00", "Z")

    def _ranks(self, source, observed_at, capi=False):
        if not isinstance(source, Mapping):
            return
        values = {}
        for journal_name, key in RANKS.items():
            capi_key = {"Explore": "explore", "Soldier": "soldier"}.get(journal_name, key)
            value = source.get(capi_key if capi else journal_name)
            if not capi and isinstance(value, (tuple, list)):
                value = value[0] if value else None
            value = number(value, 20, integer=True)
            if value is not None:
                values[key] = {"rank": value}
        self._section("ranks", values, observed_at)

    def _stats(self, source, observed_at, biology=None):
        if not isinstance(source, Mapping):
            return
        values = {}
        for journal_name, key in EXPLORATION.items():
            value = number(source.get(journal_name))
            if value is not None:
                values[key] = value
        if isinstance(biology, Mapping):
            for journal_name, key in EXOBIOLOGY.items():
                value = number(biology.get(journal_name))
                if value is not None:
                    values[key] = value
        self._section("exploration", values, observed_at)

    def _fleet(self, values, observed_at, complete=False):
        source = self._source_time("ships", observed_at)
        if source is None:
            return
        previous = copy.deepcopy(self.ships)
        previous_complete = self.fleet_complete
        if complete:
            self.ships = {
                ship["game_id"]: {**previous.get(ship["game_id"], {}), **copy.deepcopy(ship)}
                for ship in values
            }
            self.fleet_complete = True
        else:
            for ship in values:
                if ship["game_id"] not in self.ships and len(self.ships) >= 1000:
                    continue
                if ship["current"]:
                    for old in self.ships.values():
                        old["current"] = False
                self.ships[ship["game_id"]] = {**self.ships.get(ship["game_id"], {}), **ship}
            if previous != self.ships:
                # A changed current ship is only a partial fleet observation,
                # even if an earlier callback supplied a complete fleet.
                self.fleet_complete = False
        if previous == self.ships and previous_complete == self.fleet_complete:
            return
        # An explicitly empty full fleet is valid, a partial empty update is not.
        if self.ships or complete:
            self.sections["ships"] = {
                "observed_at": self._next_stamp("ships", source),
                "data": {
                    "items": sorted(
                        [
                            {"name": "", "ident": "", **copy.deepcopy(ship)}
                            for ship in self.ships.values()
                        ],
                        key=lambda ship: int(ship["game_id"]),
                    ),
                    "complete": self.fleet_complete,
                },
            }

    def journal(self, commander, is_beta, entry, state):
        if not isinstance(entry, Mapping) or not isinstance(state, Mapping):
            return False
        if not self.select(commander):
            return False
        event = entry.get("event")
        version = str(state.get("GameVersion") or entry.get("gameversion") or "")
        if event == "LoadGame":
            mode = entry.get("GameMode")
            self.blocked = bool(
                is_beta or str(mode).casefold() == "cqc" or (not mode and not entry.get("Ship"))
            )
        if (
            is_beta
            or version.startswith(("3.", "2.", "1."))
            or self.blocked
            or state.get("Captain")
        ):
            return False
        entry_commander = entry.get("Commander") or (
            entry.get("Name") if event == "Commander" else None
        )
        if entry_commander and str(entry_commander).casefold() != self.commander.casefold():
            return False
        # FID is supplied by EDMC's current state, never inferred from CAPI IDs.
        if not self._identity(commander, state.get("FID") or entry.get("FID")):
            return False
        observed_at = timestamp(entry.get("timestamp"))
        if not observed_at:
            return False
        balance = number(state.get("Credits"), integer=True)
        if balance is not None:
            self._section("credits", {"balance": balance}, observed_at)
        self._ranks(state.get("Rank"), observed_at)
        stats = state.get("Statistics")
        if event == "Statistics":
            stats = entry
        if isinstance(stats, Mapping):
            self._stats(stats.get("Exploration", {}), observed_at, stats.get("Exobiology"))
        current = normalized_ship(
            {
                "ShipID": state.get("ShipID"),
                "ShipType": state.get("ShipType"),
                "Name": state.get("ShipName"),
                "ShipIdent": state.get("ShipIdent"),
            },
            current=True,
        )
        if (
            event == "StoredShips"
            and isinstance(entry.get("ShipsHere"), list)
            and isinstance(entry.get("ShipsRemote"), list)
        ):
            raw = entry["ShipsHere"] + entry["ShipsRemote"]
            if len(raw) < 1000:
                fleet = [normalized_ship(ship) for ship in raw]
                # Do not publish a destructive full list if any record was malformed.
                if all(fleet):
                    if current:
                        fleet = [
                            ship for ship in fleet if ship["game_id"] != current["game_id"]
                        ] + [current]
                    self._fleet(fleet, observed_at, complete=bool(current))
        if current:
            self._fleet([current], observed_at)
        return True

    def capi(self, data, is_beta=False, source_live=True):
        if (
            is_beta
            or not source_live
            or self.blocked
            or not self.fid
            or not isinstance(data, Mapping)
        ):
            return False
        commander = data.get("commander")
        if (
            not isinstance(commander, Mapping)
            or str(commander.get("name", "")).casefold() != str(self.commander).casefold()
        ):
            return False
        requested = getattr(data, "request_cmdr", None)
        if requested and str(requested).casefold() != self.commander.casefold():
            return False
        observed_at = utc_now()
        balance = number(commander.get("credits"), integer=True)
        if balance is not None:
            self._section("credits", {"balance": balance}, observed_at)
        self._ranks(commander.get("rank"), observed_at, capi=True)
        current_id = ship_id(commander.get("currentShipId"))
        current = normalized_ship(data.get("ship"), current=True, capi=True)
        if current:
            current_id = current["game_id"]
        raw = data.get("ships")
        if isinstance(raw, (Mapping, list)) and len(raw) < 1000:
            entries = raw.items() if isinstance(raw, Mapping) else enumerate(raw)
            fleet = [normalized_ship(ship, capi=True, fallback_id=key) for key, ship in entries]
            if all(fleet):
                for ship in fleet:
                    ship["current"] = ship["game_id"] == current_id
                if current and not any(ship["game_id"] == current_id for ship in fleet):
                    fleet.append(current)
                self._fleet(fleet, observed_at, complete=True)
        elif current:
            self._fleet([current], observed_at)
        return True

    def snapshot(self):
        if not self.commander or not self.fid or self.blocked or not self.sections:
            return None
        return {
            "version": 1,
            "commander": {"name": self.commander, "fid": self.fid},
            "sections": copy.deepcopy(self.sections),
        }
