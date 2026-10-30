# EDHQ for EDMarketConnector

Opt-in commander syncing for [ED Headquarters](https://edhq.io), version **0.2.0**.
Requires EDMC 6.1.x; no additional packages or administrator rights.

## Install

1. Download [edhq-edmc.zip](https://edhq.io/downloads/edhq-edmc.zip).
2. In EDMC, open Settings → Plugins → Open plugins folder. Close EDMC.
3. Extract so the plugins folder contains `edhq/load.py`; restart EDMC.
4. Create a sync token in your [EDHQ account](https://edhq.io/#commanders).
5. Log into the live game. In EDMC Settings → ED Headquarters, paste the token,
   enable sync, and save. Never enter a Frontier password here.

Each commander has separate settings and starts disabled. The first sync binds
an EDHQ account to that Frontier ID. A different identity pauses uploads;
accounts never relink automatically. Tokens stay in EDMC's local configuration,
so protect that file like a password. Revoke tokens on EDHQ to reject uploads.

## Choose what to sync

The existing switch syncs ranks (including reported progress), credit balance,
basic owned ships, and cumulative exploration/exobiology statistics.

Every additional category starts **off**. Select only what you want: assets and
loans; reputation, engineers, Powerplay and permits; materials/locker/backpack
and cargo; ship modules/engineering, stored modules and ship locations; suits
and weapons; carrier status; full numeric statistics; community goals; current
location; or travel, mission, combat, financial and rank history.

Location options disclose systems/stations and, when reported, surface
coordinates. Combat history includes opponent names from your own incidents.
Statistics, carrier data, loadouts and financial options can contain values or
balances. Mission/community-goal data can contain destinations. No chat, friend
lists, raw journals, Frontier credentials or authentication tokens are uploaded.

**Sync selection and public visibility are separate.** New EDHQ sections start
private; choose what to publish on EDHQ. Assets/loans use the existing Credits
visibility setting. Turning an option off removes its
unsent queued records. Already uploaded data remains until deleted on EDHQ.
An upload already in flight may finish. Disabling all syncing stops new sends.

## Delivery and coverage

The plugin uses EDMC's journal state and live CAPI callbacks, never scans journal
files. Missing/unobserved data stays unknown. Detailed ship loadouts become
available when that ship is used; the plugin cannot reconstruct old loadouts or
past journeys that were never observed. Cumulative statistics are not incremented
when journal events replay. Financial history records authoritative balances at
session boundaries, hourly, or changes of at least 10,000 Cr / 1% of the previous
recorded balance. It does not infer balances from transaction amounts.

A bounded SQLite outbox under EDMC's application folder (`edhq/outbox.sqlite3`)
keeps selected, normalized data through restarts. It contains no bearer tokens
or raw journal entries. Snapshot changes coalesce; history has stable event IDs
and retries safely. Separate commander, server and token channels never mix.
The outbox stops accepting new work at 50,000 records / 128 MiB and reports a
visible status instead of silently discarding history. Restore connectivity if
it fills. HTTPS is required except localhost; redirects are refused.

Uploads run in a background thread, normally at most every 30 seconds, split
large collections into bounded requests, and honor rate limits. Older EDHQ
servers receive the original four categories; additional data waits for a
compatible server. Legacy, beta, CQC and multicrew sessions do not sync.

The EDMC system/station URL-provider hooks open EDHQ using the configured server.
Select EDHQ as the provider in EDMC if desired. Links are resolved when clicked.

## Development

MIT licensed; runtime uses only Python's standard library and EDMC APIs.
Run `python -m unittest discover -s tests -v`, `ruff check .`, and
`ruff format --check .` from the standalone repository. Tests use synthetic data,
never player journals or real credentials. `python tests/smoke_edmc.py
--edmc-source PATH` exercises the official EDMC loader and real Tk preferences
with isolated config and networking disabled. Build with `python build.py`.

[Registry standards](https://github.com/EDCD/EDMC-Plugin-Registry/blob/main/docs/STANDARDS.md)
require continued compatibility, least privilege, readable source and ecosystem
care. Registry listing is separate from publishing this download.
