#!/usr/bin/env python3
"""Verify soundness & completeness of the Evernote export.

Local checks (no network):
  - every indexed note has a matching .enex + .json on disk (and vice-versa)
  - ENEX well-formedness
  - note JSON sanity (title / content / timestamps present)
  - attachment integrity: MD5(file) == resource hash, and size == sizeBytes
  - expected attachment count (from note manifests) vs files on disk
  - tag consistency (tags used on notes vs tags.json vs tag-hierarchy)

Server checks (with --server, via the MCP API):
  - live active-note count vs what was exported
  - per-notebook count: server vs on-disk (catches truncation / the 1000 cap)
  - trash count (trashed notes are not exported -- reported for awareness)

Usage:
  python verify_export.py            # local checks only
  python verify_export.py --server   # also reconcile against the live server
"""
import glob
import hashlib
import json
import os
import sys
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "evernote-export")
MAN = os.path.join(OUT, "_manifest")


def load(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def md5_file(path, buf=1 << 20):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(buf), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    server = "--server" in sys.argv
    idx = load(os.path.join(MAN, "notes-index.json"))
    idx_ids = {n.get("noteId") or n.get("guid") for n in idx}

    json_files = [p for p in glob.glob(os.path.join(OUT, "**", "*.json"), recursive=True)
                  if "_manifest" not in p]
    enex_files = glob.glob(os.path.join(OUT, "**", "*.enex"), recursive=True)
    print(f"index={len(idx_ids)} notes  |  on disk: {len(json_files)} json, "
          f"{len(enex_files)} enex")

    # ---------- load notes ----------
    notes = {}
    for p in json_files:
        d = load(p)
        notes[d.get("id") or d.get("noteId")] = (p, d)
    disk_ids = set(notes)

    # ---------- completeness: index <-> disk ----------
    missing = idx_ids - disk_ids
    extra = disk_ids - idx_ids
    print(f"\n[completeness] indexed but missing on disk : {len(missing)}")
    for m in list(missing)[:10]:
        print("    MISSING", m)
    print(f"[completeness] on disk but not in index    : {len(extra)}")

    # ---------- soundness: json/enex pairing ----------
    unpaired = {os.path.splitext(p)[0] for p in json_files} ^ \
               {os.path.splitext(p)[0] for p in enex_files}
    print(f"[soundness]    json/enex unpaired          : {len(unpaired)}")
    for u in list(unpaired)[:10]:
        print("    UNPAIRED", u)

    # ---------- soundness: ENEX well-formed ----------
    enex_bad = []
    for p in enex_files:
        try:
            r = ET.parse(p).getroot()
            assert r.tag == "en-export" and r.find("note/title") is not None
        except Exception as e:
            enex_bad.append((p, str(e)[:60]))
    print(f"[soundness]    ENEX well-formed            : "
          f"{len(enex_files) - len(enex_bad)}/{len(enex_files)}")
    for p, e in enex_bad[:10]:
        print("    BAD ENEX", p, e)

    # ---------- soundness: note content sanity ----------
    no_title = sum(1 for _, (p, d) in notes.items() if not d.get("title"))
    no_content = sum(1 for _, (p, d) in notes.items() if not d.get("content"))
    no_dates = sum(1 for _, (p, d) in notes.items()
                   if not (d.get("created") or d.get("createdAt")))
    unreadable = sum(1 for _, (p, d) in notes.items() if d.get("_unreadable"))
    print(f"[soundness]    notes missing title={no_title} content={no_content} "
          f"dates={no_dates}  (known-unreadable stubs={unreadable})")

    # ---------- soundness: attachment integrity (MD5 + size) ----------
    exp = present = md5ok = md5bad = sizemis = 0
    problems = []
    print("\n[attachments] recomputing MD5 of every downloaded file "
          "(this scans ~1.8 GB)...")
    for nid, (p, d) in notes.items():
        res = d.get("resources") or []
        if not res:
            continue
        adir = os.path.splitext(p)[0] + "_attachments"
        for r in res:
            exp += 1
            h = r.get("hash")
            if not h:
                continue
            matches = glob.glob(os.path.join(glob.escape(adir), h[:8] + "_*"))
            if not matches:
                problems.append(("MISSING_FILE", nid, h))
                continue
            present += 1
            fpath = matches[0]
            if md5_file(fpath) == h:
                md5ok += 1
            else:
                md5bad += 1
                problems.append(("MD5_MISMATCH", nid, h))
            sz = r.get("sizeBytes")
            if sz and os.path.getsize(fpath) != sz:
                sizemis += 1
                problems.append(("SIZE_MISMATCH", nid, h))
    print(f"[attachments] expected={exp} present={present} missing={exp - present} "
          f"| md5_ok={md5ok} md5_mismatch={md5bad} size_mismatch={sizemis}")
    for b in problems[:15]:
        print("    ", b)

    # ---------- tags ----------
    used = set()
    for nid, (p, d) in notes.items():
        for t in (d.get("tags") or []):
            if isinstance(t, dict) and t.get("id"):
                used.add(t["id"])
    flat = load(os.path.join(MAN, "tag-hierarchy.json"))["flat"]
    mtags = {t.get("tagId") or t.get("id") for t in load(os.path.join(MAN, "tags.json"))}
    print(f"\n[tags] used-on-notes={len(used)} tags.json={len(mtags)} "
          f"hierarchy={len(flat)} used-but-missing-from-hierarchy={len(used - set(flat))}")

    # ---------- server reconciliation ----------
    if server:
        import evernote_export as E
        m = E.MCP(E.get_access_token())
        m.initialize()

        def total(q):
            r = m.call("search_notes", {"query": q, "maxResults": 1})
            return r.get("totalResultCount", 0) if isinstance(r, dict) else 0

        active = total("")
        trash = total("intrash:true")
        print(f"\n[server] live active notes={active}  exported={len(disk_ids)}  "
              f"trashed(not exported)={trash}")

        disk_by_nb = {}
        for nid, (p, d) in notes.items():
            g = d.get("notebookId")
            disk_by_nb[g] = disk_by_nb.get(g, 0) + 1
        nbs = load(os.path.join(MAN, "notebooks.json"))
        mism = 0
        for nb in nbs:
            g = nb.get("notebookId") or nb.get("guid")
            srv, dsk = total(f'nbGuid:"{g}"'), disk_by_nb.get(g, 0)
            if srv != dsk:
                mism += 1
                print(f"    MISMATCH {nb.get('label')!r}: server={srv} disk={dsk}")
        print(f"[server] per-notebook mismatches: {mism}/{len(nbs)}")

    print("\nDONE")


if __name__ == "__main__":
    main()
