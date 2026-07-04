#!/usr/bin/env python3
"""Repair pass: fetch notes/attachments that the main export missed.

Re-enumerates active notes per notebook and compares to what's on disk:
  - any active note not on disk -> fetch + write enex/json/attachments
  - any existing note with attachment files missing -> re-fetch its attachments

Safe to re-run; it only adds/overwrites the specific missing items.
"""
import glob
import json
import os

import evernote_export as E


def main():
    mcp = E.MCP(E.get_access_token())
    mcp.initialize()
    with open(os.path.join(E.OUT_DIR, "_manifest", "notebooks.json"),
              encoding="utf-8") as handle:
        nbs = json.load(handle)
    nb_map = {nb.get("notebookId") or nb.get("guid"):
              {"label": nb.get("label"), "stack": nb.get("stack")} for nb in nbs}

    json_files = [p for p in glob.glob(os.path.join(E.OUT_DIR, "**", "*.json"),
                                       recursive=True) if "_manifest" not in p]
    disk = {}
    for p in json_files:
        with open(p, encoding="utf-8") as handle:
            d = json.load(handle)
        disk[d.get("id") or d.get("noteId")] = (p, d)
    disk_ids = set(disk)

    def write_note(note, nb_gid):
        meta = nb_map.get(note.get("notebookId") or nb_gid,
                          {"label": "_unknown_notebook", "stack": None})
        stack = E._safe(meta["stack"]) if meta["stack"] else "_no_stack"
        folder = os.path.join(E.OUT_DIR, stack, E._safe(meta["label"]))
        os.makedirs(folder, exist_ok=True)
        base = f"{E._safe(note.get('title', 'Untitled'))}-{note['id'][:8]}"
        with open(os.path.join(folder, base + ".enex"), "w", encoding="utf-8") as f:
            f.write(E._build_enex(note))
        with open(os.path.join(folder, base + ".json"), "w", encoding="utf-8") as f:
            json.dump(note, f, indent=2, ensure_ascii=False)
        return E._download_attachments(mcp, note, folder, base)

    # ---- 1. missing notes ----
    seen, server_ids = set(), []
    for nb in nbs:
        g = nb.get("notebookId") or nb.get("guid")
        start = 0
        while True:
            page = mcp.call("search_notes", {"query": f'nbGuid:"{g}"',
                                             "maxResults": 100, "startIndex": start})
            hits = page.get("hits", []) if isinstance(page, dict) else []
            if not hits:
                break
            for h in hits:
                hid = h.get("noteId") or h.get("guid")
                if hid and hid not in seen:
                    seen.add(hid)
                    server_ids.append((hid, g))
            start += len(hits)
            if page.get("isLastPage") or start >= page.get("totalResultCount", 0):
                break
    missing = [(hid, g) for hid, g in server_ids if hid not in disk_ids]
    print(f"[notes] server active={len(seen)} disk={len(disk_ids)} missing={len(missing)}")
    for hid, g in missing:
        note = mcp.call("get_note", {"noteId": hid})
        if not isinstance(note, dict):
            print(f"   still unreadable: {hid}")
            continue
        n = write_note(note, g)
        print(f"   fetched: {note.get('title')!r} ({hid[:8]}) +{n} attachments")

    # ---- 2. missing attachments on existing notes ----
    fixed = 0
    for nid, (p, d) in disk.items():
        res = d.get("resources") or []
        if not res:
            continue
        base = os.path.splitext(p)[0]
        adir = base + "_attachments"
        miss = [r for r in res if r.get("hash")
                and not glob.glob(os.path.join(glob.escape(adir), r["hash"][:8] + "_*"))]
        if miss:
            got = E._download_attachments(mcp, d, os.path.dirname(p),
                                          os.path.basename(base))
            print(f"   re-fetch {d.get('title')!r} ({nid[:8]}): "
                  f"{len(miss)} were missing -> downloaded {got}/{len(res)} total")
            fixed += 1
    print(f"[attachments] notes repaired: {fixed}")
    print("DONE")


if __name__ == "__main__":
    main()
