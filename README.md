# Evernote export

A small, dependency-free toolkit that exports an entire Evernote account to the
local disk through the **official Evernote MCP server** (`https://mcp.evernote.com/mcp`),
then verifies and repairs that export. Everything is stdlib-only Python (3.10+ on
Windows, but nothing here is platform-specific).

This repository is now structured as a proper Python project with:
- a package-style CLI entry point
- environment-based configuration for the token cache and output directory
- tests for redaction and configuration behavior
- no secrets written to source control or exposed in logs

There is no API key or developer waitlist involved: the scripts implement the
MCP OAuth 2.1 flow the server advertises (Dynamic Client Registration +
Authorization Code with PKCE), so the only credential is your normal Evernote
login, granted once in the browser.

## Files

| File | Purpose |
| --- | --- |
| `evernote_export.py` | Main tool: OAuth login, tool discovery, and the full export. |
| `verify_export.py` | Checks the export for completeness & soundness (local, plus optional live server reconciliation). |
| `repair_missing.py` | Re-fetches any notes/attachments the export missed. Safe to re-run. |
| `evernote-export.log` | Captured output of the last export run (for reference). |
| `evernote-export/` | The export output (see layout below). |

`verify_export.py` and `repair_missing.py` both `import evernote_export`, so all
three must stay in the same directory.

## Setup

Per the repo convention, use a virtual environment. No third-party packages are
required — the venv just keeps the interpreter isolated.

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
```

## Usage

You can run the project either as a console script or directly through the module:

```powershell
.
venv\Scripts\Activate.ps1
evernote-export login
# or
python evernote_export.py login
```

Run in order:

```powershell
# 1. One-time login. Opens a browser, you approve access, the token is cached to
#    ~/.evernote-mcp-token.json (refresh token included; auto-refreshed later).
python evernote_export.py login

# 2. (Optional) Discover the server's tools + argument schemas. Useful if the API
#    changes and the FIELD MAPPING constants in the script need adjusting.
python evernote_export.py tools

# 3. Full export -> ./evernote-export/
python evernote_export.py export

# 4. Confirm the export is complete and intact.
python verify_export.py            # local checks only
python verify_export.py --server   # also reconcile counts against the live account

# 5. If verify reports anything missing, repair it (re-runnable).
python repair_missing.py

# 6. Remove all generated export data from the configured output directory.
python evernote_export.py cleanup
```

Flag: `python evernote_export.py export --no-attachments` skips downloading
attachment bytes (metadata is still recorded).

The cleanup command removes everything under the configured output directory
(including `_manifest/`, exported `.enex`/`.json` files, and downloaded
attachments). It is useful when you want to start a fresh export without
manually deleting files from disk.

## Output layout

Everything lands under `evernote-export/`:

```
evernote-export/
  _manifest/
    notebooks.json          full notebook list (incl. stacks)
    tags.json               flat tag list as returned by the server
    tag-hierarchy.json      tags + reconstructed parent/child tree + path strings
    tag-tree.md             human-readable tag tree
    notes-index.json        every note's id/title/notebook (the enumeration cache)
    _done.txt               ledger of note ids fully exported (drives restart)
    _tag_nodes.json         persisted tag graph (drives restart)
    unreadable-notes.json   notes the server would not return
    missing-attachments.json attachments that could not be fetched
  <stack>/<notebook>/<title>-<id>.enex    one valid ENEX per note
  <stack>/<notebook>/<title>-<id>.json    full structured metadata sidecar (lossless)
  <stack>/<notebook>/<title>-<id>_attachments/<hash8>_<name>   attachment bytes
```

Notes in no stack go under `_no_stack/`. The `.json` sidecar is the lossless
record; the `.enex` is a valid, importable Evernote export (note that ENEX
flattens tags — the hierarchy lives in `tag-hierarchy.json`).

## How it works (notes for future maintenance)

- **Auth.** `login` does discovery → dynamic client registration → PKCE
  authorization-code → token, caching the result (with refresh token) to
  `~/.evernote-mcp-token.json`. Access tokens auto-refresh; expired-with-no-refresh
  forces a re-`login`.
- **Transport.** A tiny MCP client over Streamable HTTP (JSON-RPC). It throttles
  to `RATE_LIMIT_RPS` (4/s) and backs off on `429`/`5xx` and network hiccups, and
  refreshes the token mid-run on a `401`.
- **Enumeration is per-notebook.** The server caps global `search_notes` paging at
  ~1000 results, so the export pages within each notebook (`nbGuid:"..."`) to avoid
  silently missing notes. The result is cached to `notes-index.json` — delete that
  file to force re-enumeration.
- **Restartable.** Completed note ids are appended to `_manifest/_done.txt`, so a
  re-run skips finished notes and resumes where it stopped. (The last run, per
  `evernote-export.log`, exported 1489 notes + 6296 attachments with 1 failure.)
- **Tag hierarchy** is rebuilt by aggregating `parentId` edges seen across
  individual `get_note` results, then written as both a flat map, path strings, and
  a nested tree.
- **FIELD MAPPING.** Tool names (`search_notebooks`, `search_tags`, `search_notes`,
  `get_note`) are constants near the top of `evernote_export.py`. If the server's
  API changes, run `tools` and adjust them there.
</content>
</invoke>
