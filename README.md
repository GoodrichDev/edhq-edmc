# EDHQ for EDMarketConnector

Opt-in commander sync for [ED Headquarters](https://edhq.io): ranks, credit balance,
owned ships, and cumulative exploration/exobiology statistics. No Frontier password needed.

## Install

1. Download [edhq-edmc.zip](https://edhq.io/downloads/edhq-edmc.zip).
2. In EDMC, open Settings → Plugins → Open plugins folder. Close EDMC.
3. Extract there so `edhq/load.py` is directly inside the plugins folder.
4. Start EDMC. Sign into your [EDHQ account](https://edhq.io/#commanders) and create a sync token.
5. Log into your commander in the live game. In EDMC Settings → ED Headquarters,
   paste the token, enable sync, and save.

The first sync binds your EDHQ account to that commander's Frontier ID. Use a separate
EDHQ account/token for each commander. Mismatches pause uploads; accounts never relink
automatically. Each new commander starts with sync disabled.

Your profile and data start private. Choose public sections in your EDHQ account.
Revoke a token on EDHQ to reject further uploads, or disable sync/delete the token in
EDMC settings to stop sending. An upload already in flight may finish.

## Data and troubleshooting

The plugin uses EDMC's documented journal state and live CAPI callbacks. It uploads
only allowlisted profile fields: no raw journals, friends, locations, cargo, loadouts,
account passwords, or Frontier access tokens. EDHQ tokens are saved in EDMC's local
configuration; protect that file like a password. Tokens/private data are never logged.
Pending snapshots live only in memory.

Background uploads coalesce to the latest snapshot, normally at most every 30 seconds,
and retry temporary failures. Missing fields stay omitted. Statistics use cumulative
game values; replay does not increment them. Complete CAPI/StoredShips fleets replace
old records; current-ship updates preserve other ships.

“Waiting for live commander data and Frontier ID” usually clears after logging into
the live game. Legacy, beta, CQC and multicrew data do not sync. “Token rejected” means
create a fresh token. “Commander mismatch” means the token belongs to another commander
account. Website visibility controls public sections, not the fields synced.

Version `0.1.0` targets current EDMC 6.1.x callbacks. No additional packages or admin
rights are required. Source is MIT licensed. HTTPS is required except localhost
development; redirects are refused to protect tokens.

## Development

Run `python -m unittest discover -s tests -v` from the standalone repository. Tests use
synthetic data and never read player journals or transmit profiles.
`python tests/smoke_edmc.py --edmc-source PATH` tests the official release loader and
real Tk preferences with isolated config and networking disabled. Build with `python build.py`.

The [EDMC registry standards](https://github.com/EDCD/EDMC-Plugin-Registry/blob/main/docs/STANDARDS.md)
guide SemVer, naming, compatible licensing, API usage, minimal resources and readable
source. Registry listing and compatibility with future EDMC releases require ongoing review.
