import json
import os
import sys
import tempfile
import unittest
from unittest import mock

import convert_export


ENML = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<!DOCTYPE en-note SYSTEM "http://xml.evernote.com/pub/enml2.dtd">'
    '<en-note style="word-wrap: break-word;">'
    '<div>Hello <b>world</b></div>'
    '<en-todo checked="true"/> done'
    '<en-todo checked="false"/> not done'
    '<en-media hash="aaaa1111bbbb2222" type="image/jpeg" style="width: 320px;"/>'
    '<en-media hash="cccc3333dddd4444" type="application/pdf"/>'
    '<en-media hash="deadbeefdeadbeef" type="image/png"/>'  # referenced but no resource
    '</en-note>'
)


def _sample_note():
    return {
        "id": "note1",
        "title": "My <Note>",
        "content": ENML,
        "created": "2024-01-02T03:04:05.000Z",
        "updated": "2024-01-03T06:07:08.000Z",
        "notebookId": "nb1",
        "tags": [{"id": "t1", "name": "Green"}, {"id": "t2", "name": "Ideas"}],
        "attributes": {"sourceUrl": "https://example.com/page"},
        "resources": [
            {"hash": "aaaa1111bbbb2222", "name": "photo.jpg", "mime": "image/jpeg", "sizeBytes": 2048},
            {"hash": "cccc3333dddd4444", "name": "doc.pdf", "mime": "application/pdf", "sizeBytes": 700000},
            {"hash": "eeee5555ffff6666", "name": "extra.bin", "mime": "application/octet-stream", "sizeBytes": 10},
        ],
    }


def _build_source(src):
    """Write a minimal export tree under `src` and return the note dir."""
    nb_dir = os.path.join(src, "Stack", "Notebook")
    os.makedirs(nb_dir, exist_ok=True)
    base = os.path.join(nb_dir, "My _Note_-note1")
    with open(base + ".json", "w", encoding="utf-8") as fh:
        json.dump(_sample_note(), fh)
    # attachment bytes on disk (<hash8>_<name>)
    adir = base + "_attachments"
    os.makedirs(adir, exist_ok=True)
    for h, name in [("aaaa1111bbbb2222", "photo.jpg"),
                    ("cccc3333dddd4444", "doc.pdf"),
                    ("eeee5555ffff6666", "extra.bin")]:
        with open(os.path.join(adir, f"{h[:8]}_{name}"), "wb") as fh:
            fh.write(b"x")
    # a manifest dir that must be ignored by the walker
    os.makedirs(os.path.join(src, "_manifest"), exist_ok=True)
    with open(os.path.join(src, "_manifest", "notebooks.json"), "w", encoding="utf-8") as fh:
        json.dump([{"notebookId": "nb1", "label": "Notebook"}], fh)
    return base


class ConvertHtmlTests(unittest.TestCase):
    def _run(self, index=False, force=False):
        self.src = tempfile.mkdtemp()
        self.dst = tempfile.mkdtemp()
        _build_source(self.src)
        with mock.patch.object(convert_export, "get_source_dir", return_value=self.src), \
             mock.patch.object(convert_export, "get_html_dir", return_value=self.dst):
            convert_export.convert_html(force=force, index=index)
        html_path = os.path.join(self.dst, "Stack", "Notebook", "My _Note_-note1.html")
        with open(html_path, encoding="utf-8") as fh:
            return fh.read(), html_path

    def test_note_body_and_metadata(self):
        page, _ = self._run()
        # metadata header
        self.assertIn("<h1>My &lt;Note&gt;</h1>", page)
        self.assertIn("Created 2024-01-02 03:04", page)
        self.assertIn("Updated 2024-01-03 06:07", page)
        self.assertIn('href="https://example.com/page"', page)
        self.assertIn('class="en-tag">Green</span>', page)
        # ENML wrapper stripped, inner kept
        self.assertNotIn("<en-note", page)
        self.assertNotIn("<?xml", page)
        self.assertIn("Hello <b>world</b>", page)

    def test_media_todo_and_missing(self):
        page, html_path = self._run()
        # image -> <img> pointing at the copied file, style preserved
        self.assertIn('src="My _Note_-note1_attachments/aaaa1111_photo.jpg"', page)
        self.assertIn("width: 320px", page)
        # pdf -> download link with size
        self.assertIn('href="My _Note_-note1_attachments/cccc3333_doc.pdf"', page)
        self.assertIn("683.6 KB", page)
        # todo checkboxes: one checked, one not
        self.assertIn('type="checkbox" class="en-todo" disabled checked', page)
        self.assertIn('type="checkbox" class="en-todo" disabled>', page)
        # referenced hash with no resource -> visible placeholder
        self.assertIn("missing image", page)
        # non-inline resource (extra.bin) listed in footer
        self.assertIn("extra.bin", page)
        # attachments were copied into the output tree
        adir = os.path.splitext(html_path)[0] + "_attachments"
        self.assertTrue(os.path.exists(os.path.join(adir, "aaaa1111_photo.jpg")))

    def test_index_pages(self):
        self._run(index=True)
        root = os.path.join(self.dst, "index.html")
        nb = os.path.join(self.dst, "Stack", "Notebook", "index.html")
        self.assertTrue(os.path.exists(root))
        self.assertTrue(os.path.exists(nb))
        with open(root, encoding="utf-8") as fh:
            self.assertIn("Stack / Notebook", fh.read())
        with open(nb, encoding="utf-8") as fh:
            self.assertIn("My _Note_-note1.html", fh.read())

    def test_idempotent_skip_and_force(self):
        # first run writes the note
        self.src = tempfile.mkdtemp()
        self.dst = tempfile.mkdtemp()
        _build_source(self.src)
        with mock.patch.object(convert_export, "get_source_dir", return_value=self.src), \
             mock.patch.object(convert_export, "get_html_dir", return_value=self.dst):
            convert_export.convert_html()
            html_path = os.path.join(self.dst, "Stack", "Notebook", "My _Note_-note1.html")
            first_mtime = os.path.getmtime(html_path)
            # bump the html mtime so it is newer than the json -> second run must skip
            os.utime(html_path, (first_mtime + 10, first_mtime + 10))
            convert_export.convert_html()
            self.assertEqual(os.path.getmtime(html_path), first_mtime + 10)
            # force re-writes it
            convert_export.convert_html(force=True)
            self.assertNotEqual(os.path.getmtime(html_path), first_mtime + 10)

    def test_missing_source_raises(self):
        with mock.patch.object(convert_export, "get_source_dir", return_value=os.path.join(tempfile.gettempdir(), "nope-does-not-exist-xyz")):
            with self.assertRaises(SystemExit):
                convert_export.convert_html()

    def test_html_dir_env_override(self):
        with mock.patch.dict(os.environ, {"EVERNOTE_HTML_DIR": "/custom/html"}, clear=False):
            self.assertEqual(convert_export.get_html_dir(), "/custom/html")

    def test_main_rejects_unknown_format(self):
        with mock.patch.object(sys, "argv", ["convert_export.py", "pdf"]):
            with self.assertRaises(SystemExit):
                convert_export.main()

    def test_main_dispatches_html(self):
        called = {}
        with mock.patch.dict(convert_export.FORMATS, {"html": lambda force, index: called.update(force=force, index=index)}), \
             mock.patch.object(sys, "argv", ["convert_export.py", "html", "--index", "--force"]):
            convert_export.main()
        self.assertEqual(called, {"force": True, "index": True})


if __name__ == "__main__":
    unittest.main()
