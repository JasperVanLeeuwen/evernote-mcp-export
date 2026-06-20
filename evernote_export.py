#!/usr/bin/env python3
"""
Evernote export via the official Evernote MCP server (https://mcp.evernote.com/mcp).

Standalone, stdlib-only. Implements the MCP OAuth 2.1 flow the server advertises:
  - Dynamic Client Registration  (no manual API key / waitlist needed)
  - Authorization Code + PKCE (S256), public client (no client secret)
  - Streamable HTTP transport for JSON-RPC

Usage (Python 3.14 on Windows):
  python evernote_export.py login     # one-time: opens browser, stores token
  python evernote_export.py tools     # discovery: list the server's tools + schemas
  python evernote_export.py export     # export all notes + notebook/tag metadata

Output goes to ./evernote-export/ :
  _manifest/notebooks.json   full notebook list (incl. stacks)
  _manifest/tags.json        flat tag list (+ hierarchy IF the server exposes parents)
  _manifest/notes-index.json every note's guid/title/notebook
  <stack>/<notebook>/<title>-<id>.enex   one valid ENEX per note
  <stack>/<notebook>/<title>-<id>.json   full structured metadata sidecar (lossless)

NOTE ON TAG HIERARCHY: ENEX flattens tags. This script writes tags.json from whatever
the server returns; if a tag object includes a parent/parentGuid field, the hierarchy is
preserved there. Run `tools` + inspect one get_note result to confirm field names, then
adjust FIELD MAPPING below if needed.
"""

import base64
import hashlib
import http.client
import http.server
import json
import os
import re
import secrets
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
MCP_URL        = "https://mcp.evernote.com/mcp"
RESOURCE       = "https://mcp.evernote.com"
PRM_URL        = "https://mcp.evernote.com/.well-known/oauth-protected-resource"
CALLBACK_PORT  = 8765
REDIRECT_URI   = f"http://localhost:{CALLBACK_PORT}/callback"
SCOPE          = "read"
PROTOCOL_VER   = "2025-06-18"
TOKEN_FILE     = os.path.join(os.path.expanduser("~"), ".evernote-mcp-token.json")
OUT_DIR        = os.path.join(os.path.dirname(os.path.abspath(__file__)), "evernote-export")
CLIENT_NAME    = "evernote-export-script"

# Rate limiting: throttle outbound MCP requests and back off on 429/5xx.
RATE_LIMIT_RPS = 4                      # max requests/second to the server
MIN_INTERVAL   = 1.0 / RATE_LIMIT_RPS   # min seconds between requests
MAX_RETRIES    = 5                      # retries on 429 / 502 / 503 / 504
HTTP_TIMEOUT   = 20                     # per-request timeout (s); short so hiccups recover fast
WITH_ATTACHMENTS = True                 # download attachment bytes (toggle: --no-attachments)
DONE_FILE      = "_manifest/_done.txt"        # ledger of completed note ids (restart)
TAGNODES_FILE  = "_manifest/_tag_nodes.json"  # persisted tag graph (restart)

# ---------------------------------------------------------------------------
# Tiny HTTP helpers (stdlib only)
# ---------------------------------------------------------------------------
def _http(method, url, *, headers=None, data=None, form=None, json_body=None):
    h = dict(headers or {})
    body = None
    if json_body is not None:
        body = json.dumps(json_body).encode()
        h.setdefault("Content-Type", "application/json")
    elif form is not None:
        body = urllib.parse.urlencode(form).encode()
        h.setdefault("Content-Type", "application/x-www-form-urlencoded")
    elif data is not None:
        body = data
    req = urllib.request.Request(url, data=body, headers=h, method=method)
    try:
        resp = urllib.request.urlopen(req, timeout=HTTP_TIMEOUT)
        return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in e.headers.items()}, e.read()


def _get_json(url):
    status, _, body = _http("GET", url)
    if status != 200:
        raise SystemExit(f"GET {url} -> {status}: {body[:300]!r}")
    return json.loads(body)


# ---------------------------------------------------------------------------
# OAuth: discovery -> DCR -> PKCE authorization-code -> token
# ---------------------------------------------------------------------------
def _b64url(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def discover_auth_server():
    prm = _get_json(PRM_URL)
    as_url = prm["authorization_servers"][0]
    # Try RFC 8414 path, then OIDC.
    for suffix in (".well-known/oauth-authorization-server", ".well-known/openid-configuration"):
        try:
            return _get_json(as_url.rstrip("/") + "/" + suffix)
        except SystemExit:
            continue
    raise SystemExit("Could not fetch authorization server metadata")


def register_client(meta):
    body = {
        "client_name": CLIENT_NAME,
        "redirect_uris": [REDIRECT_URI],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        "scope": SCOPE,
    }
    status, _, resp = _http("POST", meta["registration_endpoint"], json_body=body)
    if status not in (200, 201):
        raise SystemExit(f"Dynamic client registration failed {status}: {resp[:400]!r}")
    return json.loads(resp)["client_id"]


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    captured = {}

    def do_GET(self):
        q = urllib.parse.urlparse(self.path).query
        params = dict(urllib.parse.parse_qsl(q))
        _CallbackHandler.captured.update(params)
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"<h2>Evernote auth complete.</h2>You can close this tab.")

    def log_message(self, *a):
        pass


def do_login():
    meta = discover_auth_server()
    client_id = register_client(meta)

    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    state = secrets.token_urlsafe(16)

    authorize = meta["authorization_endpoint"] + "?" + urllib.parse.urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPE,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })

    server = http.server.HTTPServer(("localhost", CALLBACK_PORT), _CallbackHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    print("Opening browser for Evernote login...")
    print("If it doesn't open, visit:\n" + authorize)
    webbrowser.open(authorize)

    while "code" not in _CallbackHandler.captured and "error" not in _CallbackHandler.captured:
        time.sleep(0.3)
    server.shutdown()
    cap = _CallbackHandler.captured
    if cap.get("error"):
        raise SystemExit(f"Authorization error: {cap}")
    if cap.get("state") != state:
        raise SystemExit("State mismatch -- aborting (possible CSRF).")

    status, _, resp = _http("POST", meta["token_endpoint"], form={
        "grant_type": "authorization_code",
        "code": cap["code"],
        "redirect_uri": REDIRECT_URI,
        "client_id": client_id,
        "code_verifier": verifier,
    })
    if status != 200:
        raise SystemExit(f"Token exchange failed {status}: {resp[:400]!r}")
    tok = json.loads(resp)
    tok["client_id"] = client_id
    tok["token_endpoint"] = meta["token_endpoint"]
    tok["expires_at"] = time.time() + tok.get("expires_in", 3600) - 60
    with open(TOKEN_FILE, "w") as f:
        json.dump(tok, f)
    print(f"Logged in. Token cached at {TOKEN_FILE}")


def get_access_token():
    if not os.path.exists(TOKEN_FILE):
        raise SystemExit("Not logged in. Run:  python evernote_export.py login")
    with open(TOKEN_FILE) as f:
        tok = json.load(f)
    if time.time() < tok.get("expires_at", 0):
        return tok["access_token"]
    return _refresh_token(tok)


def _refresh_token(tok):
    if "refresh_token" not in tok:
        raise SystemExit("Token expired and no refresh_token. Run `login` again.")
    status, _, resp = _http("POST", tok["token_endpoint"], form={
        "grant_type": "refresh_token",
        "refresh_token": tok["refresh_token"],
        "client_id": tok["client_id"],
        "scope": SCOPE,
    })
    if status != 200:
        raise SystemExit(f"Refresh failed {status}: {resp[:300]!r}. Run `login` again.")
    new = json.loads(resp)
    tok.update(new)  # picks up rotated refresh_token if the server rotates it
    tok["expires_at"] = time.time() + new.get("expires_in", 3600) - 60
    with open(TOKEN_FILE, "w") as f:
        json.dump(tok, f)
    return tok["access_token"]


def refresh_access_token():
    """Force a refresh from the cached token file; returns a new access token."""
    with open(TOKEN_FILE) as f:
        tok = json.load(f)
    return _refresh_token(tok)


# ---------------------------------------------------------------------------
# MCP client over Streamable HTTP
# ---------------------------------------------------------------------------
class MCP:
    def __init__(self, token):
        self.token = token
        self.session_id = None
        self._id = 0
        self._last = 0.0  # timestamp of last request, for throttling

    def _headers(self):
        h = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOL_VER,
        }
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        return h

    @staticmethod
    def _parse(headers, body):
        ctype = headers.get("content-type", "")
        text = body.decode("utf-8", "replace")
        if "text/event-stream" in ctype or text.lstrip().startswith(("event:", "data:", ":")):
            # take the last `data:` JSON payload that carries a result/error
            payload = None
            for line in text.splitlines():
                if line.startswith("data:"):
                    chunk = line[5:].strip()
                    try:
                        payload = json.loads(chunk)
                    except json.JSONDecodeError:
                        pass
            return payload
        return json.loads(text) if text.strip() else None

    def _send(self, msg):
        """POST with client-side throttle + backoff on rate-limit / transient errors."""
        status = headers = body = None
        for attempt in range(MAX_RETRIES + 1):
            wait = MIN_INTERVAL - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            try:
                status, headers, body = _http("POST", MCP_URL,
                                              headers=self._headers(), json_body=msg)
            except (urllib.error.URLError, http.client.HTTPException,
                    ConnectionError, TimeoutError, OSError) as e:
                self._last = time.time()
                if attempt < MAX_RETRIES:
                    delay = (2 ** attempt) * 0.5
                    print(f"  [net error {type(e).__name__}] backing off {delay:.1f}s "
                          f"(attempt {attempt + 1}/{MAX_RETRIES})")
                    time.sleep(delay)
                    continue
                raise SystemExit(f"network error after {MAX_RETRIES} retries: {e}")
            self._last = time.time()
            if status == 401 and b"expired" in (body or b"") and attempt < MAX_RETRIES:
                print("  [token expired] refreshing access token...")
                self.token = refresh_access_token()
                continue  # retry immediately with the fresh token
            if status in (429, 502, 503, 504) and attempt < MAX_RETRIES:
                ra = headers.get("retry-after")
                try:
                    delay = float(ra) if ra else 0.0
                except ValueError:
                    delay = 0.0
                delay = max(delay, (2 ** attempt) * 0.5)  # exp backoff floor
                print(f"  [rate-limit {status}] backing off {delay:.1f}s "
                      f"(attempt {attempt + 1}/{MAX_RETRIES})")
                time.sleep(delay)
                continue
            break
        return status, headers, body

    def _rpc(self, method, params=None, notify=False):
        msg = {"jsonrpc": "2.0", "method": method}
        if not notify:
            self._id += 1
            msg["id"] = self._id
        if params is not None:
            msg["params"] = params
        status, headers, body = self._send(msg)
        sid = headers.get("mcp-session-id")
        if sid:
            self.session_id = sid
        if status >= 400:
            raise SystemExit(f"MCP {method} -> {status}: {body[:400]!r}")
        if notify:
            return None
        parsed = self._parse(headers, body)
        if parsed and "error" in parsed:
            raise SystemExit(f"MCP {method} error: {parsed['error']}")
        return (parsed or {}).get("result")

    def initialize(self):
        res = self._rpc("initialize", {
            "protocolVersion": PROTOCOL_VER,
            "capabilities": {},
            "clientInfo": {"name": CLIENT_NAME, "version": "1.0"},
        })
        self._rpc("notifications/initialized", notify=True)
        return res

    def list_tools(self):
        return self._rpc("tools/list", {}).get("tools", [])

    def read_resource(self, uri):
        res = self._rpc("resources/read", {"uri": uri})
        return (res or {}).get("contents", [])

    def call(self, name, arguments):
        res = self._rpc("tools/call", {"name": name, "arguments": arguments})
        # Unwrap MCP content -> structured JSON when possible.
        if isinstance(res, dict):
            if "structuredContent" in res and res["structuredContent"] is not None:
                return res["structuredContent"]
            for item in res.get("content", []):
                if item.get("type") == "text":
                    try:
                        return json.loads(item["text"])
                    except (json.JSONDecodeError, KeyError):
                        return item.get("text")
        return res


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
def do_tools():
    mcp = MCP(get_access_token())
    info = mcp.initialize()
    print("Server:", json.dumps(info.get("serverInfo", {}), indent=2))
    print("\n=== TOOLS ===")
    for t in mcp.list_tools():
        print(f"\n## {t['name']}")
        print("   " + (t.get("description", "")[:200]).replace("\n", " "))
        schema = t.get("inputSchema", {})
        props = list((schema.get("properties") or {}).keys())
        print("   args:", props)
    print("\nUse this output to confirm/adjust tool names in the export() FIELD MAPPING.")


# ---- FIELD MAPPING (adjust after running `tools` if names differ) ----------
T_NOTEBOOKS = "search_notebooks"
T_TAGS      = "search_tags"
T_NOTES     = "search_notes"
T_NOTE      = "get_note"


def _safe(name, maxlen=80):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", (name or "Untitled").strip())
    return (name[:maxlen] or "Untitled").rstrip(". ")


def _enex_ts(iso):
    # ISO 8601 -> Evernote ENEX timestamp YYYYMMDDTHHMMSSZ
    if not iso:
        return None
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})", iso)
    return "".join(m.groups()) + "Z" if m else None


def _build_enex(note):
    title = note.get("title", "Untitled")
    content = note.get("content") or note.get("body") or ""
    sc = note.get("structuredContent") or note
    tags = []
    for tg in (sc.get("tags") or note.get("tags") or []):
        tags.append((tg.get("name") or tg.get("label")) if isinstance(tg, dict) else tg)
    created = _enex_ts(note.get("createdAt") or note.get("created"))
    updated = _enex_ts(note.get("updatedAt") or note.get("updated"))

    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<!DOCTYPE en-export SYSTEM "http://xml.evernote.com/pub/evernote-export4.dtd">',
             '<en-export application="evernote-export-script">',
             '  <note>',
             f'    <title>{_xml(title)}</title>',
             f'    <content><![CDATA[{content}]]></content>']
    if created: parts.append(f'    <created>{created}</created>')
    if updated: parts.append(f'    <updated>{updated}</updated>')
    for tg in tags:
        if tg: parts.append(f'    <tag>{_xml(tg)}</tag>')
    parts += ['  </note>', '</en-export>']
    return "\n".join(parts)


def _xml(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _load_done():
    p = os.path.join(OUT_DIR, DONE_FILE)
    if not os.path.exists(p):
        return set()
    with open(p, encoding="utf-8") as f:
        return {line.strip() for line in f if line.strip()}


def _mark_done(gid):
    with open(os.path.join(OUT_DIR, DONE_FILE), "a", encoding="utf-8") as f:
        f.write(gid + "\n")


def _load_tag_nodes():
    p = os.path.join(OUT_DIR, TAGNODES_FILE)
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save_tag_nodes(nodes):
    _dump(TAGNODES_FILE, nodes)


def _download_attachments(mcp, note, folder, base):
    """Fetch each resource's bytes via get_attachment -> resources/read; return count saved."""
    resources = note.get("resources") or []
    if not resources:
        return 0
    adir = os.path.join(folder, base + "_attachments")
    saved = 0
    for idx, r in enumerate(resources):
        h = r.get("hash")
        if not h:
            continue
        try:
            ga = mcp.call("get_attachment", {"noteId": note.get("id"), "hash": h})
            uri = ga.get("uri") if isinstance(ga, dict) else None
            if not uri and isinstance(ga, dict):
                for it in ga.get("content", []):
                    if it.get("uri"):
                        uri = it["uri"]
                        break
            if not uri:
                continue
            blob = None
            for c in mcp.read_resource(uri):
                if c.get("blob"):
                    blob = c["blob"]
                    break
            if blob is None:
                continue
            os.makedirs(adir, exist_ok=True)
            fname = _safe(r.get("name") or f"attachment_{idx}", 100)
            with open(os.path.join(adir, f"{h[:8]}_{fname}"), "wb") as f:
                f.write(base64.b64decode(blob))
            saved += 1
        except SystemExit as e:
            print(f"    ! attachment {h[:8]} on {note.get('id')}: {e}")
    return saved


def do_export():
    mcp = MCP(get_access_token())
    mcp.initialize()
    os.makedirs(os.path.join(OUT_DIR, "_manifest"), exist_ok=True)

    done = _load_done()             # note ids already fully exported
    tag_nodes = _load_tag_nodes()   # persisted across restarts -> full hierarchy
    if done:
        print(f"Resuming: {len(done)} notes already exported; they will be skipped.")

    # 1. Notebooks (+ stacks)
    print("Fetching notebooks...")
    nbs = mcp.call(T_NOTEBOOKS, {"query": "", "maxResults": 100})
    nb_list = nbs.get("hits", nbs) if isinstance(nbs, dict) else nbs
    nb_map = {}
    for nb in nb_list:
        gid = nb.get("notebookId") or nb.get("guid")
        nb_map[gid] = {"label": nb.get("label") or nb.get("name"), "stack": nb.get("stack")}
    _dump("_manifest/notebooks.json", nb_list)
    print(f"  {len(nb_list)} notebooks")

    # 2. Tags. search_tags gives the complete flat node set (names); per-note
    #    get_note results carry parentId, which we aggregate below to rebuild the tree.
    print("Fetching tags...")
    try:
        tags = mcp.call(T_TAGS, {"query": "", "maxResults": 100})
        tag_list = tags.get("hits", tags) if isinstance(tags, dict) else tags
        _dump("_manifest/tags.json", tag_list)
        for t in tag_list:
            tid = t.get("tagId") or t.get("id")
            tag_nodes.setdefault(tid, {"name": t.get("label") or t.get("name"),
                                       "parentId": None})
    except SystemExit as e:
        print("  (tags fetch skipped:", e, ")")

    # 3. Enumerate notes PER NOTEBOOK. The server caps global search_notes paging
    #    at ~1000 results, so a single empty query misses notes; paging within each
    #    notebook (nbGuid:"...") avoids the cap. Cached so restarts skip this.
    idx_path = os.path.join(OUT_DIR, "_manifest", "notes-index.json")
    if os.path.exists(idx_path):
        with open(idx_path, encoding="utf-8") as f:
            notes_index = json.load(f)
        print(f"Using cached notes-index ({len(notes_index)} notes); "
              f"delete it to re-enumerate.")
    else:
        print("Enumerating notes per notebook...")
        seen_ids, notes_index = set(), []
        for nb in nb_list:
            nb_gid = nb.get("notebookId") or nb.get("guid")
            start, cnt = 0, 0
            while True:
                page = mcp.call(T_NOTES, {"query": f'nbGuid:"{nb_gid}"',
                                          "maxResults": 100, "startIndex": start})
                hits = page.get("hits", []) if isinstance(page, dict) else (page or [])
                if not hits:
                    break
                for h in hits:
                    hid = h.get("noteId") or h.get("guid")
                    if hid and hid not in seen_ids:
                        seen_ids.add(hid)
                        h["notebookId"] = nb_gid
                        notes_index.append(h)
                        cnt += 1
                start += len(hits)
                total = page.get("totalResultCount", 0) if isinstance(page, dict) else 0
                is_last = page.get("isLastPage") if isinstance(page, dict) else True
                if is_last or start >= total:
                    break
            if cnt >= 1000:
                print(f"  WARNING: {nb.get('label')!r} returned {cnt} (>=1000 cap) "
                      f"-- may be truncated")
            print(f"  {nb.get('label')}: {cnt}  (total {len(notes_index)})")
        _dump("_manifest/notes-index.json", notes_index)
        print(f"Total notes enumerated: {len(notes_index)}")

    # 4. Fetch + write each note; skip already-done ids (restartable)
    todo = [n for n in notes_index if (n.get("noteId") or n.get("guid")) not in done]
    print(f"Exporting {len(todo)} notes ({len(done)} already done, "
          f"attachments={'on' if WITH_ATTACHMENTS else 'off'})...")
    written = failed = attach = 0
    for i, n in enumerate(todo, 1):
        gid = n.get("noteId") or n.get("guid")
        try:
            note = mcp.call(T_NOTE, {"noteId": gid})
            if not isinstance(note, dict):
                raise SystemExit(f"unexpected get_note payload "
                                 f"({type(note).__name__}): {str(note)[:120]}")
            for tg in (note.get("tags") or []):
                if isinstance(tg, dict) and tg.get("id"):
                    tag_nodes[tg["id"]] = {"name": tg.get("name") or tg.get("label"),
                                           "parentId": tg.get("parentId")}
            nb_gid = note.get("notebookId") or note.get("notebookGuid")
            meta = nb_map.get(nb_gid, {"label": "_unknown_notebook", "stack": None})
            stack = _safe(meta["stack"]) if meta["stack"] else "_no_stack"
            folder = os.path.join(OUT_DIR, stack, _safe(meta["label"]))
            os.makedirs(folder, exist_ok=True)
            base = f"{_safe(note.get('title','Untitled'))}-{gid[:8]}"
            with open(os.path.join(folder, base + ".enex"), "w", encoding="utf-8") as f:
                f.write(_build_enex(note))
            with open(os.path.join(folder, base + ".json"), "w", encoding="utf-8") as f:
                json.dump(note, f, indent=2, ensure_ascii=False)
            if WITH_ATTACHMENTS:
                attach += _download_attachments(mcp, note, folder, base)
            _mark_done(gid)   # ledger only after ALL files for this note are written
            done.add(gid)
            written += 1
        except SystemExit as e:
            print(f"  ! {gid}: {e}")
            failed += 1
        if i % 50 == 0:
            _save_tag_nodes(tag_nodes)
            print(f"  {i}/{len(todo)} (ok={written} fail={failed} attach={attach})")

    # 5. Reconstruct tag hierarchy from aggregated parentId edges.
    _save_tag_nodes(tag_nodes)
    for tid, node in tag_nodes.items():
        pid = node.get("parentId")
        node["parentName"] = tag_nodes.get(pid, {}).get("name") if pid else None

    def _path(tid, seen=()):
        node = tag_nodes.get(tid)
        if not node or tid in seen:
            return [node["name"]] if node else []
        pid = node.get("parentId")
        return (_path(pid, seen + (tid,)) if pid else []) + [node["name"]]

    hierarchy = {
        "flat": tag_nodes,
        "paths": {tid: " / ".join(_path(tid)) for tid in tag_nodes},
        "roots": _build_tree(tag_nodes),
    }
    _dump("_manifest/tag-hierarchy.json", hierarchy)
    with_parent = sum(1 for n in tag_nodes.values() if n.get("parentId"))
    print(f"\nTag hierarchy: {len(tag_nodes)} tags, {with_parent} with a parent "
          f"-> _manifest/tag-hierarchy.json")
    print(f"Done. Written={written} Failed={failed} Attachments={attach} "
          f"(total exported={len(done)})\nOutput: {OUT_DIR}")


def _build_tree(nodes):
    """Nested {name, children:[...]} tree from a flat id->{name,parentId} map."""
    children = {}
    for tid, n in nodes.items():
        children.setdefault(n.get("parentId"), []).append(tid)

    def build(tid):
        return {"name": nodes[tid]["name"],
                "children": [build(c) for c in sorted(
                    children.get(tid, []), key=lambda x: (nodes[x]["name"] or ""))]}

    roots = sorted([t for t in nodes if not nodes[t].get("parentId")
                    or nodes[t]["parentId"] not in nodes],
                   key=lambda x: (nodes[x]["name"] or ""))
    return [build(t) for t in roots]


def _dump(rel, obj):
    with open(os.path.join(OUT_DIR, rel), "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
def main():
    global WITH_ATTACHMENTS
    args = sys.argv[1:]
    if "--no-attachments" in args:
        WITH_ATTACHMENTS = False
        args.remove("--no-attachments")
    cmd = args[0] if args else ""
    if cmd == "login":
        do_login()
    elif cmd == "tools":
        do_tools()
    elif cmd == "export":
        do_export()
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
