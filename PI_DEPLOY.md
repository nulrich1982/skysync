# SkySync — Raspberry Pi deployment (clawdpi)

Host moved here from a Windows PC on 2026-10-05: Task Scheduler jobs only
fire while that account is logged on and awake, and the PC was silently
losing ~10hrs/day of sync coverage to sleep. clawdpi is always-on, so this is
the real production deployment now; SETUP.md (Windows/Task Scheduler) is kept
for reference, not actively used.

## Layout

Mirrors the existing Clawdbot convention on this box (`/opt/bot/...`) rather
than inventing a new one:

```
/opt/skysync/
  repo/            git clone of github.com/nulrich1982/skysync (code only)
  venv/            python3 -m venv, pip install -e ".[dev]"
  secrets/         mode 700 dir, mode 600 .bin files — see secrets model below
  config.toml      non-secret config; lives here (not in repo/) so the
                   relative "secrets" path resolves to /opt/skysync/secrets
  systemd-units/   staging copies of the installed units + the restic patch,
                   kept for reference/redeploy (the installed copies live in
                   /etc/systemd/system/, root-owned)

/mnt/vault/skysync/
  state/           ledger.sqlite3, ledger-grocery.sqlite3, heartbeat.json
  logs/            skysync.log (rotating, secrets redacted)
```

`/opt/skysync` and `/mnt/vault/skysync` both needed root once to create
(`/opt` and the `/mnt/vault` top level are root-owned), then `chown`'d to
`nulrich1982`. Everything after that is doable without sudo except installing
the systemd units themselves and patching the backup script (both root-only
paths).

## Secrets: DPAPI has no Linux equivalent here

This was a deliberate, discussed tradeoff, not an oversight — see the
`REVISED 2026-10-05` entry in DESIGN_NOTES.md and the module docstring in
`src/skysync/secrets.py`. Short version: no TPM on this Pi, no Secret Service
daemon running headless, so there's no OS-backed vault to use. Secrets are
plain bytes on disk, protected only by filesystem permissions (dir 700, file
600, owner-only) — the same trust model already protecting this Pi's other
live production secrets (`/opt/bot/secrets`, this bot's own Graph tokens).
`src/skysync/secrets.py` auto-selects DPAPI on Windows / permissions-only on
Linux by `sys.platform`; the Windows path is untouched.

## Auth: both logins are headless-friendly, done once directly on the Pi

Neither needs a GUI browser on the Pi itself:

* **Microsoft To Do**: MSAL device-code flow. `python -m skysync.main
  --config /opt/skysync/config.toml login` prints a URL + short code;
  approve it from any browser, phone or laptop.
* **Skylight**: pure scripted HTTP (reverse-engineered Rails-form + OAuth
  PKCE flow, no real browser involved at all). Seed `skylight_email` /
  `skylight_password` via `skysync.secrets set` first (these prompt with
  input hidden — run them yourself, don't paste a password through an
  agent), then `python -m skysync.skylight.oauth login --config
  /opt/skysync/config.toml`.

Re-run either only if its refresh token is ever revoked — both auto-rotate
every run after that.

## Ledger migration (not a fresh start)

The SQLite ledger is the system of record for idempotency — starting fresh
on the Pi risked re-deriving ~2,400 items from scratch and duplicating
anything the original first-dry-run-1,351-planned-creates problem (see
DESIGN_NOTES.md) would have hit again. Instead: disabled both Windows
scheduled tasks to freeze a consistent snapshot, `scp`'d `ledger.sqlite3` +
`ledger-grocery.sqlite3` + `heartbeat.json` over as-is, pointed
`[general].state_dir` at `/mnt/vault/skysync/state`. First dry-run on the Pi
came back with `planned_writes: []` — confirming a clean, seamless handoff.

## Scheduling: systemd timers, not cron

Mirrors the existing `token-watch.timer`/`.service` pattern on this box.

* `skysync.timer` → `skysync.service`: `OnCalendar=*:0/15`, runs `skysync.main
  run --live`.
* `skysync-menu-monthend.timer` → `skysync-menu-monthend.service`:
  `OnCalendar=*-01,02,03,04,05,08,09,10,11,12~01 21:00:00` (last day of
  every month, August through May — verified with `systemd-analyze calendar
  --iterations=12` to confirm June/July are correctly skipped and each
  month's *actual* last day is hit, not a fixed day-of-month).

Both services set `OnFailure=digest-alert@%n.service` — reusing this box's
existing generic failure notifier (pulls the failed unit's own journal,
pings healthchecks.io and/or emails via Graph if `/opt/bot/secrets/
alerts.env` has those configured; never fails itself, so it can't recurse).
No new alerting infrastructure needed.

Unit files are version-controlled at `task/systemd/*.service` /
`task/systemd/*.timer` in this repo; installed copies in
`/etc/systemd/system/` are the source of truth at runtime — after editing
the versioned copies, re-deploy with:

```bash
scp task/systemd/*.service task/systemd/*.timer clawdpi:/opt/skysync/systemd-units/
ssh clawdpi 'sudo cp /opt/skysync/systemd-units/*.service /opt/skysync/systemd-units/*.timer /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl restart skysync.timer skysync-menu-monthend.timer'
```

## Backup

`/opt/bot/scripts/restic-backup.sh` (root-owned, nightly, encrypts
client-side to the 2TB drive) was extended to also cover `/opt/skysync/
secrets`, `/mnt/vault/skysync`, and the four new unit files — same treatment
as this box's other irreplaceable material (refresh tokens, state files).
Every edit to that script was verified against a throwaway copy first
(exact diff + `bash -n` syntax check) before ever touching the live file —
it protects data from other services too (treasurer, masonic, family
digest), so no edit to it was made blind.

## Redeploying code after a change

```bash
ssh clawdpi
cd /opt/skysync/repo && git pull --ff-only
/opt/skysync/venv/bin/pip install -q -e ".[dev]"
cd /opt/skysync/repo && /opt/skysync/venv/bin/python -m pytest tests/ -q -o addopts="" --color=no
```

No service to restart — the timers invoke a fresh `python -m skysync...`
each run, so a `git pull` takes effect on the next scheduled fire
automatically.

## Decommissioning the Windows side

Both Windows scheduled tasks (`SkySync`, `SkySync-MenuMonthEnd`) were
disabled, not deleted, during the cutover — `Enable-ScheduledTask` brings
either back if the Pi ever needs to be taken down for maintenance. Running
both simultaneously against the same remote accounts with *separate* local
ledgers would cause duplicate/conflicting writes, so **never re-enable the
Windows tasks while the Pi's timers are also active.**
