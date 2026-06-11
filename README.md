# SkySync

Self-hosted two-way task sync for a household, on a personal Microsoft account:

```
   Microsoft To Do  ⇄  Skylight Calendar
   (Graph, delegated)   (unofficial API)
          └── local sync ledger = system of record ──┘
```

A task created/edited/completed/deleted in either source propagates to the
other — assigned to the right family member on the Skylight frame — with no
duplicates and no sync loops, driven by Windows Task Scheduler every 15 min.
(An optional SharePoint-list master for work/school tenants is supported; see
the SETUP.md appendix.)

> ⚠️ The Skylight API is **unofficial**: it may break without notice and its
> use may conflict with Skylight's Terms of Service. See `SETUP.md`.

* **[SETUP.md](SETUP.md)** — Azure app registration, SharePoint list schema,
  Skylight frameId capture, secret seeding (DPAPI), first-run auth, Task
  Scheduler install, monitoring.
* **[DESIGN_NOTES.md](DESIGN_NOTES.md)** — architecture, constraint
  decisions, conflict/delete semantics, accepted edges.

## Quick reference

```powershell
python -m pip install -e ".[dev]"
python -m pytest -q                                  # offline test suite
python -m skysync.main --config config.toml login    # one-time Graph auth
python -m skysync.main --config config.toml run --mock     # fixtures only
python -m skysync.main --config config.toml run --dry-run  # plan, no writes
python -m skysync.main --config config.toml run --live
python -m skysync.main --config config.toml status
.\register-task.ps1                                  # install scheduled task
.\check-heartbeat.ps1                                # dead-man's-switch check
```

## Layout

| Path | What |
|---|---|
| `src/skysync/engine.py`, `ledger.py` | reconciliation engine + crash-safe ledger (the core) |
| `src/skysync/graph/` | delegated MSAL auth, To Do + SharePoint clients |
| `src/skysync/skylight/` | typed client generated from the vendored OpenAPI spec |
| `src/skysync/secrets.py` | DPAPI secret store (`python -m skysync.secrets ...`) |
| `fixtures/`, `tests/` | mock fixtures + the full offline test suite |
| `.claude/agents/` | project subagents (implementer / mechanic / engine-reviewer) |
