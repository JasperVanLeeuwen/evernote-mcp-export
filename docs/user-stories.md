# User Stories

This document is the source of truth for what the toolkit does and why. **Every
change starts here**: add or update a story before writing code (see the workflow
in [CLAUDE.md](../CLAUDE.md)).

Each story follows: _As a **role**, I want **capability**, so that **outcome**._
Acceptance criteria are written so they map directly onto tests.

---

## Epic: Own my Evernote data

### US-1 — Authenticate once

**As an** Evernote user, **I want** to log in through my normal browser session
**so that** I can grant access without an API key, developer waitlist, or storing
a password.

Acceptance criteria:
- `login` runs the MCP OAuth 2.1 flow (Dynamic Client Registration + Authorization
  Code with PKCE) and opens the browser for approval.
- The resulting token (including refresh token) is cached to the configured token
  file; no secret is printed to stdout or logs.
- Access tokens auto-refresh; an expired token with no refresh token forces a
  re-`login` rather than failing silently.

### US-2 — Export everything, losslessly

**As an** Evernote user, **I want** to export every note, notebook, tag, and
attachment to local disk **so that** I have a complete, portable copy I control.

Acceptance criteria:
- Every note is enumerated **per notebook** (to defeat the server's ~1000-result
  global paging cap) and cached to `_manifest/notes-index.json`.
- Each note is written twice: a lossless `.json` sidecar and an importable `.enex`.
- Tag hierarchy is reconstructed from `parentId` edges and written to
  `_manifest/tag-hierarchy.json` (since `.enex` flattens tags).
- Attachment bytes are downloaded unless `--no-attachments` is passed.

### US-3 — Resume an interrupted export

**As an** Evernote user with a large account, **I want** a re-run to skip work
already done **so that** a dropped connection doesn't force me to start over.

Acceptance criteria:
- Completed note ids are appended to `_manifest/_done.txt` and skipped on re-run.
- The tag graph persists across runs via `_manifest/_tag_nodes.json`.
- Transport throttles to `RATE_LIMIT_RPS`, backs off on 429/5xx, and refreshes the
  token mid-run on a 401.

### US-4 — Trust the export is complete

**As an** Evernote user, **I want** to verify the export is complete and intact
**so that** I can trust it before deleting anything from Evernote.

Acceptance criteria:
- `verify_export.py` runs local structural checks with no network.
- `--server` reconciles local counts against the live account.
- Anything missing is reported clearly enough to act on.

### US-5 — Repair gaps

**As an** Evernote user, **I want** to re-fetch only what the export missed
**so that** I can close gaps without a full re-export.

Acceptance criteria:
- `repair_missing.py` fetches only notes/attachments flagged as missing.
- It is safe to re-run (idempotent).

### US-6 — Read my notes without Evernote

**As an** Evernote user, **I want** to convert the export into browsable HTML
**so that** I can read and share my notes offline, without any Evernote software.

Acceptance criteria:
- `convert_export.py html` transforms the `.json` sidecars entirely offline (no
  auth, no network).
- Inline objects translate: images render as `<img>`, other attachments as
  download links, `<en-todo>` as disabled checkboxes.
- Attachments are copied (not linked back to the source) so the output tree is
  self-contained and zippable.
- Re-runs skip up-to-date HTML unless `--force`; `--index` writes per-notebook and
  root index pages.
- New output formats can be added via the `FORMATS` registry without changing the
  driver.

### US-7 — Start fresh

**As an** Evernote user, **I want** to remove all generated export data
**so that** I can start a clean export.

Acceptance criteria:
- `cleanup` removes everything under the configured output directory (manifest,
  `.enex`/`.json`, attachments).
