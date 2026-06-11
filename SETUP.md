# SkySync — Setup

Two-way household task sync, self-hosted on an always-on Windows machine:
**SharePoint list (system of record) ⇄ Microsoft To Do ⇄ Skylight Calendar.**

> ## ⚠️ Risk note — read this first
> The Skylight Calendar API is **unofficial and reverse-engineered**. It is not
> documented, not supported, **may break without notice**, and using it **may
> conflict with Skylight's Terms of Service**. SkySync validates every Skylight
> response and fails loudly when the API drifts, but you accept the breakage
> risk (and the ToS question) by running this. The Microsoft Graph legs are
> fully supported APIs.

---

## What you must provide (checklist)

| # | Item | Where it goes |
|---|------|---------------|
| 1 | Azure **tenant ID** | `config.toml` → `[graph].tenant_id` |
| 2 | Azure app **client ID** | `config.toml` → `[graph].client_id` |
| 3 | SharePoint **site ID** + **list ID** | `config.toml` → `[sharepoint]` |
| 4 | Skylight **email** (+ password, or a captured token) | DPAPI secret store |
| 5 | Skylight **frameId** | `config.toml` → `[skylight].frame_id` |
| 6 | To Do list names + Skylight category labels per child | `config.toml` → `[mapping.children.*]` |

## 0. Prerequisites

* Windows 10/11 or Server, always on, with network access.
* Python 3.11+ (`python --version`).
* From the repo root: `python -m pip install -e ".[dev]"`, then
  `python -m pytest -q` (everything should pass offline).
* `copy config.example.toml config.toml` and edit as you go below.

> **OneDrive note:** if this folder is inside OneDrive, consider pointing
> `[general].state_dir` and `log_dir` at a non-synced local path (e.g.
> `C:\ProgramData\SkySync\state`) — sync engines and SQLite don't mix well.

## 1. Azure app registration (Graph)

1. [entra.microsoft.com](https://entra.microsoft.com) → **App registrations → New registration**.
   * Name: `SkySync`; supported accounts: *single tenant* is fine.
   * **No redirect URI is needed** — SkySync uses the **device-code flow**.
2. **Authentication** → *Allow public client flows* → **Yes** (required for device code).
3. **API permissions** → add **Delegated**:
   * `Tasks.ReadWrite` (Microsoft To Do)
   * `Sites.ReadWrite.All` (SharePoint leg, delegated default)
   * (`offline_access` is requested automatically by MSAL at sign-in.)
4. **Admin consent**: on a work/school tenant, `Sites.ReadWrite.All` (and possibly
   everything) needs an admin to press **Grant admin consent**. On your own
   tenant you are that admin.
5. Copy the **Directory (tenant) ID** and **Application (client) ID** into
   `config.toml`.

**Optional app-only SharePoint leg** (`[graph].sharepoint_auth = "app_only"`):
also add **Application** permission `Sites.ReadWrite.All`, grant admin consent,
create a **client secret**, and seed it:
`python -m skysync.secrets set graph_client_secret`. To Do **always** stays
delegated — Graph does not support app-only To Do access. Trade-offs in
`DESIGN_NOTES.md`.

## 2. SharePoint list

Create a list on the site of your choice with these columns (internal names
must match exactly):

| Column | Type | Notes |
|--------|------|-------|
| `Title` | built-in | task title |
| `Notes` | multiple lines (plain text) | |
| `DueDate` | date | date-only |
| `Assignee` | single line of text | matches `sp_assignee` in config |
| `Status` | choice: `open`, `completed` | default `open` |
| `InternalId` | single line of text | **SkySync's marker — don't touch** |

> Create columns from list settings so the *internal* name matches (create as
> `Notes`, `DueDate`, etc. directly — renaming later keeps the old internal name).

Get the IDs (sign into [Graph Explorer](https://aka.ms/ge)):

```
GET https://graph.microsoft.com/v1.0/sites/{hostname}:/sites/{sitePath}?$select=id
GET https://graph.microsoft.com/v1.0/sites/{site-id}/lists?$select=id,displayName
```

Put `site_id` (the full `host,guid,guid` string) and `list_id` in `config.toml`.

## 3. Microsoft To Do lists

Create one list per child in To Do (e.g. *Avery's Chores*) plus keep the
default *Tasks* list (catch-all for unmapped assignees). Put the display names
in `[mapping.children.*].todo_list` and `[todo].default_list`.

## 4. Skylight frameId (DevTools/HAR capture)

1. Sign in at **app.ourskylight.com** in a desktop browser.
2. Open DevTools (F12) → **Network** tab → reload.
3. Filter requests for `frames/` — you'll see calls like
   `https://app.ourskylight.com/api/frames/4418006/chores?...`.
   The number after `/frames/` is your **frameId** → `[skylight].frame_id`.
4. While you're there (optional, more robust than password auth): click any
   `api/...` request → **Headers** → copy the `Authorization: Basic <token>`
   value for step 5. (Saving a HAR and searching it works too.)
5. Family members: Frame settings → categories. Each child's **category
   label** goes in `[mapping.children.*].skylight_category`.

## 5. Seed secrets (DPAPI)

Secrets are encrypted with Windows DPAPI for the **current user** and stored
as `secrets\*.bin` (gitignored; useless on any other machine/account).

**Do this logged in as the account the scheduled task will run as.**

```powershell
# password mode (SkySync logs in via POST /api/sessions):
python -m skysync.secrets set skylight_email
python -m skysync.secrets set skylight_password

# OR token mode (paste the captured Authorization value; preferred):
python -m skysync.secrets set skylight_token

# only if sharepoint_auth = "app_only":
python -m skysync.secrets set graph_client_secret

python -m skysync.secrets list
python -m skysync.secrets check skylight_token   # decrypts, prints length only
```

## 6. First-run Graph auth (device code)

```powershell
python -m skysync.main --config config.toml login
```

Follow the printed instructions (open the URL, enter the code, sign in as the
Microsoft account that owns the To Do lists and can edit the SharePoint list).
The token cache — including the refresh token, which **rotates on every
subsequent run** — is stored DPAPI-encrypted. You should never need to log in
again unless the refresh token is revoked or expires from long disuse.

## 7. Smoke tests

```powershell
python -m skysync.main --config config.toml run --mock      # offline fixtures
python -m skysync.graph.cli --config config.toml todo-dump  # live To Do read
python -m skysync.graph.cli --config config.toml sp-dump    # live SP read
python -m skysync.skylight.cli --config config.toml dump    # live Skylight read
python -m skysync.main --config config.toml run --dry-run   # plans, no writes
python -m skysync.main --config config.toml run --live      # first real sync
python -m skysync.main --config config.toml status
```

Review the dry-run's `planned_writes` before going live.

## 8. Task Scheduler install (runs whether logged on or not)

From an **elevated** PowerShell, as the secret-seeding account:

```powershell
.\register-task.ps1
```

It reads the cadence from `[schedule].interval_minutes` (default 15), creates
the task with **Run whether user is logged on or not** (you'll be prompted
once for the account password — Task Scheduler stores it, not SkySync),
ignores overlapping starts, and exports `task\SkySync.xml`.

DPAPI gotcha: if runs fail with `CryptUnprotectData failed`, the task is
running as a different account than the one that seeded the secrets.

## 9. Monitoring (dead-man's switch)

* `state\heartbeat.json` is rewritten **only after successful live runs**.
* `.\check-heartbeat.ps1 [-Popup]` exits non-zero when the heartbeat is older
  than 45 minutes — schedule it hourly for a local alert.
* Or set `[heartbeat].ping_url` to a [healthchecks.io](https://healthchecks.io)
  check URL: it's pinged on success, and their service emails you when pings stop.
* Logs: `logs\skysync.log` (rotating, secrets redacted).

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `AuthError: no cached account` | Run step 6 (`login`). |
| `AuthError: silent token acquisition failed` | Refresh token expired/revoked — run `login` again. |
| `CryptUnprotectData failed` | Secrets seeded by a different Windows account — re-seed as the task account. |
| `SchemaDriftError: ...` | The unofficial Skylight API changed. Re-capture a HAR, compare with `src/skysync/skylight/spec/`, update models/client. |
| `another run appears active` | Previous run still going (or crashed <60 min ago); the lock self-heals when stale. |
| Chores missing on the frame | Assignee not mapped to a Skylight category — check `[mapping.children]` and the log line naming the skipped task. |
