# SkySync — Setup

Two-way household task sync, self-hosted on an always-on Windows machine:

**Microsoft To Do ⇄ Skylight Calendar** (personal Microsoft account).

The local sync ledger is the system of record — there is no cloud master to
maintain. (A SharePoint master for work/school tenants is supported as an
option; see the appendix.)

> ## ⚠️ Risk note — read this first
> The Skylight Calendar API is **unofficial and reverse-engineered**. It is not
> documented, not supported, **may break without notice**, and using it **may
> conflict with Skylight's Terms of Service**. SkySync validates every Skylight
> response and fails loudly when the API drifts, but you accept the breakage
> risk (and the ToS question) by running this. The Microsoft Graph leg is a
> fully supported API.

---

## What you must provide (checklist)

| # | Item | Where it goes |
|---|------|---------------|
| 1 | Entra app **client ID** (personal-account app registration) | `config.toml` → `[graph].client_id` |
| 2 | Skylight **token** (or email + password) | DPAPI secret store |
| 3 | Skylight **frameId** | `config.toml` → `[skylight].frame_id` |
| 4 | To Do list names + Skylight category labels per child | `config.toml` → `[mapping.children.*]` |

## 0. Prerequisites

* Windows 10/11 or Server, always on, with network access.
* Python 3.11+ (`python --version`).
* From the repo root: `python -m pip install -e ".[dev]"`, then
  `python -m pytest -q` (everything should pass offline).
* `copy config.example.toml config.toml` and edit as you go below.

> **OneDrive note:** if this folder is inside OneDrive, consider pointing
> `[general].state_dir` and `log_dir` at a non-synced local path (e.g.
> `C:\ProgramData\SkySync\state`) — sync engines and SQLite don't mix well.

## 1. Entra app registration (personal Microsoft account)

You register the app while signed in with your **personal** Microsoft account
(this creates/uses your account's default directory — no work tenant involved).

1. Go to [entra.microsoft.com](https://entra.microsoft.com), signing in with
   the personal account that owns the family's To Do lists → **App
   registrations → New registration**.
   * Name: `SkySync`.
   * **Supported account types: "Personal Microsoft accounts only"** (or "…and
     personal Microsoft accounts" — it must include personal).
   * Leave the redirect URI **empty** — SkySync uses the device-code flow.
2. **Authentication** → *Advanced settings* → **Allow public client flows =
   Yes** → Save. (Required for device-code login.)
3. **API permissions** → Add a permission → Microsoft Graph → **Delegated** →
   `Tasks.ReadWrite`. (That's the only one. `offline_access` is requested
   automatically at sign-in; no admin consent exists or is needed for
   personal accounts.)
4. From **Overview**, copy the **Application (client) ID** into
   `config.toml` → `[graph].client_id`, and leave
   `[graph].tenant_id = "consumers"` (that's the personal-accounts authority,
   not a placeholder).

## 2. Microsoft To Do lists

In the To Do app/site signed in as that same personal account, create one
list per child (e.g. *Avery's Chores*) and keep the built-in *Tasks* list —
it's the catch-all for unmapped assignees (`[todo].default_list`). Put the
display names in `[mapping.children.*].todo_list`.

## 3. Skylight frameId + token (DevTools/HAR capture)

1. Sign in at **app.ourskylight.com** in a desktop browser.
2. Open DevTools (F12) → **Network** tab → reload.
3. Filter requests for `frames/` — you'll see calls like
   `https://app.ourskylight.com/api/frames/4418006/chores?...`.
   The number after `/frames/` is your **frameId** → `[skylight].frame_id`.
4. Recommended: click any `api/...` request → **Headers** → Request Headers →
   copy the full `authorization:` value for step 4 below — it will look like
   `Bearer xyz...` or `Basic xyz...`; copy it **including the scheme word**,
   SkySync sends it exactly as captured. (Saving a HAR and searching it works
   too.)
5. Family members: Frame settings → categories. Each child's **category
   label** goes in `[mapping.children.*].skylight_category`.

## 4. Seed secrets (DPAPI)

Secrets are encrypted with Windows DPAPI for the **current user** and stored
as `secrets\*.bin` (gitignored; useless on any other machine/account).

**Do this logged in as the account the scheduled task will run as.**

```powershell
# token mode (paste the captured Authorization value; preferred):
python -m skysync.secrets set skylight_token

# OR password mode (SkySync logs in via POST /api/sessions):
python -m skysync.secrets set skylight_email
python -m skysync.secrets set skylight_password

python -m skysync.secrets list
python -m skysync.secrets check skylight_token   # decrypts, prints length only
```

## 5. First-run Graph auth (device code)

```powershell
python -m skysync.main --config config.toml login
```

Follow the printed instructions (open the URL, enter the code, sign in with
the **personal** account that owns the To Do lists). The token cache —
including the refresh token, which **rotates on every subsequent run** — is
stored DPAPI-encrypted. You should never need to log in again unless the
refresh token is revoked or expires from long disuse.

## 6. Smoke tests

```powershell
python -m skysync.main --config config.toml run --mock      # offline fixtures
python -m skysync.graph.cli --config config.toml todo-dump  # live To Do read
python -m skysync.skylight.cli --config config.toml dump    # live Skylight read
python -m skysync.main --config config.toml run --dry-run   # plans writes, executes none
```

Review the dry-run's `planned_writes` — it should list exactly the creates
you expect. If it looks right:

```powershell
python -m skysync.main --config config.toml run --live
python -m skysync.main --config config.toml status
```

Then check the Skylight frame — your To Do tasks should appear under the
right kids.

## 7. Task Scheduler install (runs whether logged on or not)

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

## 8. Monitoring (dead-man's switch)

* `state\heartbeat.json` is rewritten **only after successful live runs**.
* `.\check-heartbeat.ps1 [-Popup]` exits non-zero when the heartbeat is older
  than 45 minutes — schedule it hourly for a local alert.
* Or set `[heartbeat].ping_url` to a [healthchecks.io](https://healthchecks.io)
  check URL: it's pinged on success, and their service emails you when pings stop.
* Logs: `logs\skysync.log` (rotating, secrets redacted).

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `AuthError: no cached account` | Run step 5 (`login`). |
| `AuthError: silent token acquisition failed` | Refresh token expired/revoked — run `login` again. |
| `CryptUnprotectData failed` | Secrets seeded by a different Windows account — re-seed as the task account. |
| `SchemaDriftError: ...` | The unofficial Skylight API changed. Re-capture a HAR, compare with `src/skysync/skylight/spec/`, update models/client. |
| `another run appears active` | Previous run still going (or crashed <60 min ago); the lock self-heals when stale. |
| Chores missing on the frame | Assignee not mapped to a Skylight category — check `[mapping.children]` and the log line naming the skipped task. |
| Tasks deleted on the frame come back? | They don't — frame deletes *detach* (by design). Tasks deleted in **To Do** remove the chore. |

---

## Appendix: optional SharePoint master (work/school tenants)

The codebase still supports a three-way mode with a SharePoint list as the
system of record. Requirements beyond the steps above: a Microsoft 365
work/school tenant; `[graph].tenant_id` set to the tenant GUID; delegated
`Sites.ReadWrite.All` added to the app registration (admin consent on work
tenants); the `[sharepoint]` section uncommented with site/list IDs (get them
via Graph Explorer: `GET /sites/{hostname}:/sites/{path}?$select=id`, then
`GET /sites/{site-id}/lists`); an `sp_assignee` per child mapping; and a list
with columns `Title, Notes(text), DueDate(date), Assignee(text),
Status(choice: open/completed), InternalId(text)` — internal names exact.
App-only auth for the SharePoint leg is available via
`[graph].sharepoint_auth = "app_only"` plus a seeded `graph_client_secret`.
