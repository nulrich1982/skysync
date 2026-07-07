# SkySync — Design Notes

Decisions, justifications, and known edges. Companion to `SETUP.md`.

## June 2026 revision: SharePoint master dropped (owner decision)

The original brief made a SharePoint list the system of record. The owner
keeps personal and work (markwellsvcs) domains separate, and personal
Microsoft accounts have no SharePoint — so the deployed configuration is
**two-way To Do ⇄ Skylight with the local SQLite ledger as the system of
record**. The engine was already side-generic; the SharePoint adapter remains
in the codebase and re-activates by adding a `[sharepoint]` config section
(see SETUP.md appendix). Consequences in two-way mode:
* auth needs only `Tasks.ReadWrite` on the `consumers` authority;
* in simultaneous-edit conflicts, To Do (timestamped) beats Skylight
  (timestampless), and `sharepoint_wins` degrades to most-recent-wins;
* the ledger file (`state/ledger.sqlite3`) is the durable record — keep
  `state_dir` out of OneDrive sync and back it up if you care about history.

## Architecture in one paragraph

Three *side adapters* (SharePoint, To Do, Skylight) implement one `TaskClient`
protocol ([models.py](src/skysync/models.py)). The engine
([engine.py](src/skysync/engine.py)) never touches raw API payloads: each side
converts its objects to a `CanonicalTask` and exposes a **projection** — the
subset of canonical fields that side can represent. Delta detection hashes
projections per side and compares against the ledger's last-seen hashes;
conflict resolution, delete policies, and propagation operate on canonical
state, with SharePoint ordered first as the system of record. All writes are
journaled in the ledger (`pending_ops`) before execution.

## July 2026: Skylight OAuth (hands-off auth)

Manually captured Skylight bearer tokens expire ~weekly, which meant recurring
manual re-seeding (and one silent multi-day outage). Skylight's mobile client
actually uses **OAuth 2.0 Authorization-Code + PKCE** (public client
`skylight-mobile`, no secret; flow reverse-engineered by the community project
`andreabedini/skylight-cli`). `src/skysync/skylight/oauth.py` implements it:

* **One-time login** (`python -m skysync.skylight.oauth login`): hit
  `/oauth/authorize` first (server stashes the request against the session),
  then the hosted Rails form login (`/auth/session`), then follow the redirect
  chain back through `/oauth/authorize` to the `skylight-family://welcome?code=`
  custom-scheme redirect; exchange the code + PKCE verifier at `/oauth/token`.
  Plain `requests` — no browser (a `requests` session won't follow the custom
  scheme, so we read the code from the Location header).
* **Ongoing**: `get_access_token` returns a DPAPI-cached 2-hour access token,
  refreshing via `grant_type=refresh_token` when near expiry. Refresh tokens
  **rotate on every use**; the new one is persisted *before* anything else can
  fail (same crash-safety rule as the Graph refresh token). Same-run 401 forces
  one refresh + retry.

The client auth precedence is: OAuth refresh token (if present) → captured
`skylight_token` (legacy) → password mode (dead — `/api/sessions` is the
version-gated mobile endpoint). This retired an earlier parked Playwright
browser-login attempt (Skylight's cross-domain login→app handshake never
completed under automation).

## June 2026 addition: grocery-list pairing

A second engine instance mirrors one Skylight LIST (default "Grocery List")
to one To Do list, with its own ledger (`state/ledger-grocery.sqlite3`) and
its own policy: no due-date stamping (groceries are dateless), deletes mirror
both ways by default (`[grocery].mirror_deletes`), and — unlike windowed
chores — list items are fetched in full, so absence IS authoritative
(`SyncPolicy.sky_absence_trusted`). Adapter:
[list_adapter.py](src/skysync/skylight/list_adapter.py); projection is
title+status only. Also: Cloudflare fronts the Skylight API and 403-blocks
non-browser-looking requests; the client presents browser-like headers, and
captured Authorization values are sent verbatim with whichever scheme
(`Bearer`/`Basic`) was captured.

## Constraint-driven decisions

### 1. To Do leg: delegated MSAL only
Graph offers no app-only path for `/me/todo`; the leg uses
`PublicClientApplication` + device code, scopes `Tasks.ReadWrite` (+
`offline_access`, added by MSAL automatically). The serialized MSAL cache —
which contains the refresh token — is persisted to the DPAPI store after
**every** `acquire_token_silent` call, because AAD rotates refresh tokens and
losing a rotation kills unattended setups weeks later
([auth.py](src/skysync/graph/auth.py), `_persist_cache`).

### SharePoint leg: delegated by default (app-only available)
The exception allowed client-credentials for SharePoint. Default is
**delegated on the same signed-in user** because: (a) the To Do leg forces a
delegated login to exist anyway, so app-only adds a second credential (client
secret) without removing the first; (b) if the delegated refresh token dies,
the sync is down regardless of how SharePoint authenticates — splitting auth
buys no resilience; (c) fewer secrets, least privilege on one family account.
`[graph].sharepoint_auth = "app_only"` switches to
`ConfidentialClientApplication` + `Sites.ReadWrite.All` application permission
for tenants where the signing user can't get delegated site access.

### 2. Skylight: unofficial API, fail loud
The vendored OpenAPI spec (`src/skysync/skylight/spec/`, from
github.com/TheEagleByte/skylight-api) drives `tools/generate_skylight_models.py`,
which asserts expected fields still exist in the spec and emits the pydantic
models in `models_generated.py`. Every response is validated; mismatch ⇒
`SchemaDriftError` ⇒ the run aborts before any dependent write. Notes:
* The spec path `"/chores/{choreId}reate_multiple"` is a HAR-conversion
  artifact; the real creation endpoint is `POST /chores/create_multiple`.
* Auth is an opaque token sent as `Authorization: Basic <token>` (NOT
  base64(email:password)); obtained from `POST /api/sessions` or captured
  from DevTools. Password mode re-logins once on 401; token mode fails with
  instructions to re-capture.
* Chores carry **no modification timestamp** and **no free-text field** —
  this shapes conflict handling and recovery (below).
* npm cross-check (`@eaglebyte/skylight-mcp`): registry page wasn't reachable
  from the build environment (403); the vendored spec's example payloads were
  used as ground truth instead. Worth re-checking if drift appears.

### 3. Ledger + crash safety (the highest-risk core)
SQLite ([ledger.py](src/skysync/ledger.py)), `synchronous=FULL`. One row per
task keyed by `internal_id` (uuid4) storing all remote ids, assignee +
skylight category, status, canonical JSON + `content_hash`, per-side
projection hashes, `last_synced`, and a tombstone flag.

* **Idempotency keys**: none of the three APIs support real idempotency keys
  on create, so SkySync embeds the `internal_id` as a *marker* where possible
  — SharePoint `InternalId` column, To Do `linkedResources.externalId` — and
  journals every write in `pending_ops` (planned → executing → done/failed).
  The op's ledger mutation commits **atomically with** its 'done' mark.
* **Replay**: an op left 'executing' by a crash is resolved on the next run by
  *searching* the remote side: marker lookup, else (Skylight has no marker) an
  exact-projection match restricted to items **no ledger row owns**. Adopt,
  never re-create. Interrupted updates/deletes are simply re-derived — writes
  are absolute (full desired state), so replaying them is harmless.
* **Loop-proofing**: a side is only written when its projection hash differs
  from the desired canonical projection; our own writes hash equal on
  read-back and are absorbed silently. No timestamps are involved in change
  detection, so there is no echo loop. A remote that normalizes values (e.g.
  truncates a title) appears once as an inbound change and converges.
* **Ledger loss**: rebuilding from an empty ledger re-binds via markers and
  content matching with zero remote writes (tested).

### 4. Windows-local only
No cloud components. Task Scheduler runs `python -m skysync.main run --live`
every 15 min (configurable), "whether user is logged on or not". Overlap is
prevented twice: Task Scheduler `IgnoreNew` + a stale-aware lock file.
Monitoring is a local heartbeat file + optional outbound ping — the machine
needs no inbound access.

### 5. Secrets
DPAPI (`CryptProtectData`, user scope, app entropy) via ctypes — no extra
dependency. Plain Credential Manager was rejected because MSAL's token cache
can exceed credential blob size limits; DPAPI files have no such limit and the
.gitignore (committed first) excludes them. A log `RedactionFilter` masks
Authorization headers, JWTs, and long base64 runs as a backstop.

## Conflict & delete semantics (config decisions #3/#4)

* **Winner-takes-all per conflict**: when two+ sides changed since last sync,
  the most-recently-modified side wins **wholly** (its representable fields);
  the discarded side's would-be state is logged at WARNING. Field-level
  merging of disjoint edits was deliberately rejected — it's untestable
  against the stated policy and risks Frankenstein tasks. Skylight exposes no
  timestamps, so in mixed conflicts it loses to timestamped sides; pure ties
  resolve sp > todo > skylight.
* **Deletes** (defaults): To Do delete ⇒ propagates (chore and SP row
  removed — keeping the master row would resurrect the task); Skylight delete
  ⇒ **detaches only** (a kid clearing the frame can't destroy the backlog;
  the row stops managing Skylight); SharePoint delete ⇒ propagates (it's the
  master). With propagation switched off for To Do/SP, a deleted item is
  re-created next run — "the canonical task still exists" is the honest
  meaning of not propagating deletes; only Skylight gets a true detach flag.
* **Completions** always propagate everywhere.

## Field-fidelity rules worth knowing

* **Notes** exist on SP/To Do only; a notes-only edit never writes Skylight.
* **Assignee** is canonical lowercase; sides reverse-map their labels/lists.
  An assignee no side-mapping covers (e.g. "grandma") still syncs SP ⇄ To Do
  (default list) and is *skipped* on Skylight; a To Do edit can't clobber an
  assignee it can't represent (`representable()` guard).
* **Undated tasks**: Skylight's chore chart is date-based and its PUT can't
  clear a start date, so any undated task Skylight holds/should hold gets
  `due = today` stamped and propagated. Documented policy, prevents an
  un-clearable-date write loop.
* **Recurring/routine chores** are Skylight-native (RRULE instances) and are
  not synced (config `sync_recurring`).
* **Windowed fetch**: chores are listed `today − 14 d … today + 60 d`;
  absence outside that window (or of an undated chore) is never treated as a
  delete. A due date moved from outside-window to outside-window in one hop
  can leave a stale chore date on the frame until it re-enters the window —
  accepted edge.
* **To Do "moves"**: Graph can't move tasks between lists; an assignee change
  is create+delete with a fresh id. The marker heals a crash between the two
  (stray duplicate detected and removed).

## Stack deviations
None. Python 3.11+ (`msal`, `requests`, `pydantic` v2, `PyYAML` for the spec
parser, `pytest`), PowerShell for scheduling glue. `requests` over `httpx`:
synchronous workload, battle-tested, one fewer moving part.

## Objections / accepted risks
* No objection to any hard constraint; all five implemented as specified.
* Accepted risks, by choice: Skylight ToS/drift (prominent in SETUP.md);
  winner-takes-all conflicts; the windowed-absence edge above; `--dry-run`
  plans against a copy of the ledger, so a dry run between two live runs can
  show plans the next live run would order slightly differently (recovery
  ops); completed tasks are kept in sync forever (no archival) — a future
  `archive_completed_after_days` knob is the natural extension.
