# Evernote export

A small, dependency-free toolkit that exports an entire Evernote account to the
local disk through the **official Evernote MCP server** (`https://mcp.evernote.com/mcp`),
then verifies and repairs that export. Everything is stdlib-only Python (3.10+ on
Windows, but nothing here is platform-specific).

This repository is now structured as a proper Python project with:
- an installable console entry point
- environment-based configuration for the token cache and output directory
- automated tests covering security, configuration, and export flows
- no secrets written to source control or exposed in logs

There is no API key or developer waitlist involved: the scripts implement the
MCP OAuth 2.1 flow the server advertises (Dynamic Client Registration +
Authorization Code with PKCE), so the only credential is your normal Evernote
login, granted once in the browser.

## Files

| File | Purpose |
| --- | --- |
| `evernote_export.py` | Main tool: OAuth login, tool discovery, the full export, and cleanup. |
| `verify_export.py` | Checks the export for completeness & soundness (local, plus optional live server reconciliation). |
| `repair_missing.py` | Re-fetches any notes/attachments the export missed. Safe to re-run. |
| `convert_export.py` | Converts the export into other formats (currently HTML). Offline, no auth. |
| `evernote-export/` | Default export output location (override with `EVERNOTE_EXPORT_DIR`). |
| `evernote-export-html/` | Default HTML conversion output (override with `EVERNOTE_HTML_DIR`). |

`verify_export.py`, `repair_missing.py`, and `convert_export.py` all
`import evernote_export`, so they must stay in the same directory.

## Setup

Per the repo convention, use a virtual environment. No third-party packages are
required — the venv just keeps the interpreter isolated.

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
python -m pip install -e .
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

# 6. (Optional) Convert the export to a browsable HTML tree.
python convert_export.py html            # -> ./evernote-export-html/
python convert_export.py html --index    # also write per-notebook + root index.html

# 7. Remove all generated export data from the configured output directory.
python evernote_export.py cleanup
```

Flag: `python evernote_export.py export --no-attachments` skips downloading
attachment bytes (metadata is still recorded).

The cleanup command removes everything under the configured output directory
(including `_manifest/`, exported `.enex`/`.json` files, and downloaded
attachments). It is useful when you want to start a fresh export without
manually deleting files from disk.

## Configuration

The following environment variables override the defaults:

- `EVERNOTE_MCP_TOKEN_FILE`: path to the cached OAuth token JSON file
- `EVERNOTE_EXPORT_DIR`: directory where the export is written
- `EVERNOTE_HTML_DIR`: directory where the HTML conversion is written

If unset, the token cache defaults to `~/.evernote-mcp-token.json`, the export
output defaults to `./evernote-export/`, and the HTML conversion defaults to
`./evernote-export-html/`, all under the repository root.

## Output layout

Everything lands under the configured output directory (default: `./evernote-export/`):

```
evernote-export/
  _manifest/
    notebooks.json          full notebook list (incl. stacks)
    tags.json               flat tag list as returned by the server
    tag-hierarchy.json      tags + reconstructed parent/child tree + path strings
    notes-index.json        every note's id/title/notebook (the enumeration cache)
    _done.txt               ledger of note ids fully exported (drives restart)
    _tag_nodes.json         persisted tag graph (drives restart)
  <stack>/<notebook>/<title>-<id>.enex    one valid ENEX per note
  <stack>/<notebook>/<title>-<id>.json    full structured metadata sidecar (lossless)
  <stack>/<notebook>/<title>-<id>_attachments/<hash8>_<name>   attachment bytes
```

Notes in no stack go under `_no_stack/`. The `.json` sidecar is the lossless
record; the `.enex` is a valid, importable Evernote export (note that ENEX
flattens tags — the hierarchy lives in `tag-hierarchy.json`).

## Convert to other formats

`convert_export.py` reads the lossless per-note `.json` sidecars and writes a
**separate, self-contained output tree** in the target format. It runs entirely
offline — no login or network is needed, since conversion is a pure local
transform of what `export` already downloaded.

```powershell
python convert_export.py html            # convert every note -> HTML
python convert_export.py html --index    # also write per-notebook + root index.html
python convert_export.py html --force    # re-convert even if the .html is up-to-date
```

HTML output mirrors the source layout under `./evernote-export-html/` (override
with `EVERNOTE_HTML_DIR`):

```
evernote-export-html/
  <stack>/<notebook>/<title>-<id>.html                        one self-contained HTML per note
  <stack>/<notebook>/<title>-<id>_attachments/<hash8>_<name>  copied attachment bytes
  <stack>/<notebook>/index.html                               (with --index) per-notebook index
  index.html                                                  (with --index) root index
```

Each note becomes a standalone HTML page with a header (title, created/updated
dates, tags, and source URL when present). Inline Evernote objects are
translated: images (`<en-media>` of an image type) render as `<img>`, other
attachments (PDFs, etc.) render as download links, and checkboxes (`<en-todo>`)
render as disabled checkboxes. Attachments are **copied** into the HTML tree and
linked relatively, so the whole `evernote-export-html/` folder can be zipped and
shared as-is. Re-runs skip notes whose HTML is already up-to-date (use `--force`
to override).

Like the source export, the converted output is personal data and is git-ignored.
The conversion is format-pluggable (a small `FORMATS` registry in
`convert_export.py`), so additional targets — e.g. Markdown — can be added
alongside `html` without changing the driver.

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
  re-run skips finished notes and resumes where it stopped.
- **Tag hierarchy** is rebuilt by aggregating `parentId` edges seen across
  individual `get_note` results, then written as both a flat map, path strings, and
  a nested tree.
- **FIELD MAPPING.** Tool names (`search_notebooks`, `search_tags`, `search_notes`,
  `get_note`) are constants near the top of `evernote_export.py`. If the server's
  API changes, run `tools` and adjust them there.
</content>
</invoke>
