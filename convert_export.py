#!/usr/bin/env python3
"""Convert an Evernote export into other formats (starting with HTML).

Offline and stdlib-only. Reads the lossless per-note JSON sidecars written by
`evernote_export.py` and produces a parallel output tree in the target format.
No network or auth is needed -- conversion is a pure local transform.

Currently supported formats: html.

Usage:
  python convert_export.py html                 # convert every note -> HTML
  python convert_export.py html --index         # also write per-notebook + root index.html
  python convert_export.py html --force         # re-convert even if the .html is up-to-date

Input  (default: ./evernote-export/, override with EVERNOTE_EXPORT_DIR):
  <stack>/<notebook>/<title>-<id>.json                          lossless note sidecar
  <stack>/<notebook>/<title>-<id>_attachments/<hash8>_<name>    attachment bytes

Output (default: ./evernote-export-html/, override with EVERNOTE_HTML_DIR):
  <stack>/<notebook>/<title>-<id>.html                          one self-contained HTML per note
  <stack>/<notebook>/<title>-<id>_attachments/<hash8>_<name>    copied attachment bytes
  <stack>/<notebook>/index.html                                 (with --index) per-notebook index
  index.html                                                    (with --index) root index

The HTML tree is self-contained (attachments are copied, links are relative), so
it can be zipped and shared. Like the source export, it is personal data and is
git-ignored -- see .gitignore.
"""
import html
import json
import os
import re
import shutil
import sys

import evernote_export

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DEFAULT_HTML_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "evernote-export-html")


def get_source_dir():
    """Where the JSON/ENEX export lives (produced by evernote_export.py)."""
    return evernote_export.get_output_dir()


def get_html_dir():
    return os.environ.get("EVERNOTE_HTML_DIR", DEFAULT_HTML_DIR)


# ---------------------------------------------------------------------------
# ENML -> HTML
# ---------------------------------------------------------------------------
_ATTR_RE = re.compile(r'([\w:-]+)\s*=\s*"([^"]*)"')
_XMLDECL_RE = re.compile(r"<\?xml[^>]*\?>", re.IGNORECASE)
_DOCTYPE_RE = re.compile(r"<!DOCTYPE[^>]*>", re.IGNORECASE)
_ENNOTE_OPEN_RE = re.compile(r"<en-note\b[^>]*>", re.IGNORECASE)
_ENNOTE_CLOSE_RE = re.compile(r"</en-note\s*>", re.IGNORECASE)
_ENMEDIA_RE = re.compile(r"<en-media\b([^>]*?)/?>", re.IGNORECASE)
_ENTODO_RE = re.compile(r"<en-todo\b([^>]*?)/?>", re.IGNORECASE)
_ENCRYPT_RE = re.compile(r"<en-crypt\b[^>]*>.*?</en-crypt\s*>", re.IGNORECASE | re.DOTALL)


def _attrs(s):
    return {k.lower(): v for k, v in _ATTR_RE.findall(s or "")}


def _human_size(n):
    try:
        n = float(n)
    except (TypeError, ValueError):
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def enml_to_html_body(content, resources, used_hashes, attach_dir):
    """Transform an ENML note body into an HTML fragment.

    `resources` is the note's resource list (each with hash/name/mime/sizeBytes).
    `attach_dir` is the note-relative folder its attachments were copied into.
    Hashes rendered inline are added to `used_hashes` so the caller can list any
    leftover (non-inline) attachments in a footer.
    """
    by_hash = {r.get("hash"): r for r in (resources or []) if r.get("hash")}

    def media_sub(m):
        a = _attrs(m.group(1))
        h = a.get("hash")
        r = by_hash.get(h)
        mime = a.get("type") or (r.get("mime") if r else "") or ""
        if r:
            used_hashes.add(h)
            fname = f"{h[:8]}_{r.get('name') or h}"
        elif h:
            fname = None  # referenced hash we have no file for
        else:
            return "<div class=\"en-missing\">[embedded object]</div>"
        rel = f"{attach_dir}/{fname}" if fname else None
        style = html.escape(a.get("style", ""), quote=True)
        if mime.startswith("image/"):
            if not rel:
                return "<div class=\"en-missing\">[missing image]</div>"
            alt = html.escape(a.get("alt") or (r.get("name") if r else "") or "", quote=True)
            return f'<img class="en-media" src="{html.escape(rel, quote=True)}" alt="{alt}" style="{style}">'
        # non-image: render a download link (or placeholder if the file is absent)
        label = html.escape((r.get("name") if r else None) or mime or "attachment")
        size = _human_size(r.get("sizeBytes")) if r else ""
        meta = f" <span class=\"en-size\">({html.escape(mime)}{', ' + size if size else ''})</span>" if mime else ""
        if not rel:
            return f'<div class="en-missing">[missing attachment: {label}]</div>'
        return (f'<div class="en-attachment">📎 '
                f'<a href="{html.escape(rel, quote=True)}">{label}</a>{meta}</div>')

    def todo_sub(m):
        a = _attrs(m.group(1))
        checked = " checked" if (a.get("checked", "").lower() == "true") else ""
        return f'<input type="checkbox" class="en-todo" disabled{checked}>'

    body = content or ""
    body = _XMLDECL_RE.sub("", body)
    body = _DOCTYPE_RE.sub("", body)
    body = _ENCRYPT_RE.sub('<div class="en-crypt">[encrypted content -- not exported]</div>', body)
    body = _ENMEDIA_RE.sub(media_sub, body)
    body = _ENTODO_RE.sub(todo_sub, body)
    # unwrap <en-note ...>...</en-note> into a plain container
    body = _ENNOTE_OPEN_RE.sub("", body)
    body = _ENNOTE_CLOSE_RE.sub("", body)
    return body


_CSS = """
:root { color-scheme: light dark; }
body { font-family: -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif;
       line-height: 1.5; max-width: 820px; margin: 2rem auto; padding: 0 1rem; }
.en-header { border-bottom: 1px solid #8884; padding-bottom: .75rem; margin-bottom: 1.25rem; }
.en-header h1 { margin: 0 0 .35rem; font-size: 1.5rem; }
.en-meta { font-size: .85rem; opacity: .75; }
.en-meta a { color: inherit; }
.en-tags { margin-top: .4rem; }
.en-tag { display: inline-block; font-size: .75rem; padding: .1rem .5rem; margin: .1rem .2rem .1rem 0;
          border: 1px solid #8886; border-radius: 999px; }
.en-body img, .en-media { max-width: 100%; height: auto; }
.en-attachment { margin: .5rem 0; padding: .4rem .6rem; border: 1px solid #8884; border-radius: 6px;
                 display: inline-block; }
.en-size { opacity: .6; font-size: .85em; }
.en-missing, .en-crypt { color: #b00; font-style: italic; }
.en-footer { border-top: 1px solid #8884; margin-top: 2rem; padding-top: .75rem; font-size: .85rem; }
.en-index li { margin: .2rem 0; }
"""


def _fmt_date(iso):
    if not iso:
        return ""
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})", iso)
    if not m:
        return html.escape(iso)
    y, mo, d, hh, mm = m.groups()
    return f"{y}-{mo}-{d} {hh}:{mm}"


def render_note_html(data, attach_dir):
    title = data.get("title") or "Untitled"
    resources = data.get("resources") or []
    used = set()
    body = enml_to_html_body(data.get("content"), resources, used, attach_dir)

    created = _fmt_date(data.get("created") or data.get("createdAt"))
    updated = _fmt_date(data.get("updated") or data.get("updatedAt"))
    attrs = data.get("attributes") or {}
    source_url = attrs.get("sourceUrl")

    tags = []
    for t in (data.get("tags") or []):
        name = t.get("name") or t.get("label") if isinstance(t, dict) else t
        if name:
            tags.append(name)

    meta_bits = []
    if created:
        meta_bits.append(f"Created {html.escape(created)}")
    if updated and updated != created:
        meta_bits.append(f"Updated {html.escape(updated)}")
    if source_url:
        u = html.escape(source_url, quote=True)
        meta_bits.append(f'Source: <a href="{u}">{html.escape(source_url)}</a>')

    tags_html = ""
    if tags:
        chips = "".join(f'<span class="en-tag">{html.escape(t)}</span>' for t in tags)
        tags_html = f'<div class="en-tags">{chips}</div>'

    # footer: attachments not shown inline
    footer = ""
    leftover = [r for r in resources if r.get("hash") and r["hash"] not in used]
    if leftover:
        items = []
        for r in leftover:
            h = r["hash"]
            fname = f"{h[:8]}_{r.get('name') or h}"
            rel = html.escape(f"{attach_dir}/{fname}", quote=True)
            label = html.escape(r.get("name") or r.get("mime") or "attachment")
            size = _human_size(r.get("sizeBytes"))
            extra = f" <span class=\"en-size\">({size})</span>" if size else ""
            items.append(f'<li>📎 <a href="{rel}">{label}</a>{extra}</li>')
        footer = ('<div class="en-footer"><strong>Attachments</strong>'
                  f'<ul>{"".join(items)}</ul></div>')

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>{_CSS}</style>
</head>
<body>
<header class="en-header">
<h1>{html.escape(title)}</h1>
<div class="en-meta">{' &middot; '.join(meta_bits)}</div>
{tags_html}
</header>
<article class="en-body">
{body}
</article>
{footer}
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------
def _iter_note_json(src):
    for root, _dirs, files in os.walk(src):
        if "_manifest" in os.path.relpath(root, src).split(os.sep):
            continue
        for f in files:
            if f.endswith(".json"):
                yield os.path.join(root, f)


def convert_html(force=False, index=False):
    src = get_source_dir()
    dst = get_html_dir()
    if not os.path.isdir(src):
        raise SystemExit(f"Source export not found: {src}\n"
                         f"Run `python evernote_export.py export` first.")

    entries = []   # (rel_html_path, title, created, stack, notebook)
    written = skipped = failed = 0
    for jpath in _iter_note_json(src):
        rel = os.path.relpath(jpath, src)
        rel_html = os.path.splitext(rel)[0] + ".html"
        out_path = os.path.join(dst, rel_html)
        src_attach = os.path.splitext(jpath)[0] + "_attachments"

        if (not force and os.path.exists(out_path)
                and os.path.getmtime(out_path) >= os.path.getmtime(jpath)):
            skipped += 1
            _record_entry(entries, rel_html, jpath, src)
            continue
        try:
            with open(jpath, encoding="utf-8") as fh:
                data = json.load(fh)
            attach_dir = os.path.basename(os.path.splitext(rel_html)[0]) + "_attachments"
            page = render_note_html(data, attach_dir)
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            with open(out_path, "w", encoding="utf-8") as fh:
                fh.write(page)
            if os.path.isdir(src_attach):
                shutil.copytree(src_attach, os.path.splitext(out_path)[0] + "_attachments",
                                dirs_exist_ok=True)
            entries.append(_entry_from(rel_html, data, rel))
            written += 1
        except (OSError, ValueError) as e:
            print(f"  ! {rel}: {e}")
            failed += 1
        if (written + skipped) % 200 == 0:
            print(f"  {written + skipped} processed (written={written} skipped={skipped})")

    print(f"HTML: written={written} skipped={skipped} failed={failed} -> {dst}")

    if index:
        _write_indexes(dst, entries)
        print(f"Indexes written under {dst}")


def _entry_from(rel_html, data, rel):
    parts = rel.split(os.sep)
    stack = parts[0] if len(parts) > 1 else "_no_stack"
    notebook = parts[1] if len(parts) > 2 else parts[0]
    return {
        "href": rel_html.replace(os.sep, "/"),
        "title": data.get("title") or "Untitled",
        "created": data.get("created") or data.get("createdAt") or "",
        "stack": stack,
        "notebook": notebook,
    }


def _record_entry(entries, rel_html, jpath, src):
    try:
        with open(jpath, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return
    entries.append(_entry_from(rel_html, data, os.path.relpath(jpath, src)))


def _rel_from(base_dir, target, dst):
    return os.path.relpath(os.path.join(dst, target), base_dir).replace(os.sep, "/")


def _write_indexes(dst, entries):
    # group -> stack -> notebook -> [entries]
    tree = {}
    for e in entries:
        tree.setdefault(e["stack"], {}).setdefault(e["notebook"], []).append(e)

    # per-notebook index.html
    for stack, notebooks in tree.items():
        for notebook, items in notebooks.items():
            nb_dir = os.path.join(dst, stack, notebook)
            if not os.path.isdir(nb_dir):
                continue
            items = sorted(items, key=lambda x: x["title"].lower())
            rows = "".join(
                f'<li><a href="{html.escape(_rel_from(nb_dir, e["href"], dst), quote=True)}">'
                f'{html.escape(e["title"])}</a> '
                f'<span class="en-size">{html.escape(_fmt_date(e["created"]))}</span></li>'
                for e in items)
            _write_index_page(os.path.join(nb_dir, "index.html"),
                              f"{stack} / {notebook}", rows, len(items))

    # root index.html
    blocks = []
    for stack in sorted(tree):
        for notebook in sorted(tree[stack]):
            items = tree[stack][notebook]
            nb_index = f"{stack}/{notebook}/index.html".replace(os.sep, "/")
            blocks.append(
                f'<li><a href="{html.escape(nb_index, quote=True)}">'
                f'{html.escape(stack)} / {html.escape(notebook)}</a> '
                f'<span class="en-size">({len(items)})</span></li>')
    _write_index_page(os.path.join(dst, "index.html"), "Evernote export",
                      "".join(blocks), len(entries))


def _write_index_page(path, heading, rows, count):
    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(heading)}</title>
<style>{_CSS}</style>
</head>
<body>
<header class="en-header"><h1>{html.escape(heading)}</h1>
<div class="en-meta">{count} item(s)</div></header>
<ul class="en-index">{rows}</ul>
</body>
</html>
"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(page)


# ---------------------------------------------------------------------------
FORMATS = {"html": convert_html}


def main():
    args = sys.argv[1:]
    force = "--force" in args
    index = "--index" in args
    args = [a for a in args if not a.startswith("--")]
    fmt = args[0] if args else ""
    if fmt not in FORMATS:
        print(__doc__)
        print(f"\nSupported formats: {', '.join(sorted(FORMATS))}")
        sys.exit(1)
    FORMATS[fmt](force=force, index=index)


if __name__ == "__main__":
    main()
