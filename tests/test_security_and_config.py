import base64
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
from pathlib import Path
from unittest import mock

import evernote_export
import repair_missing
import verify_export


class FakeMCP:
    def __init__(self, token):
        self.token = token
        self.calls = []

    def initialize(self):
        return {"serverInfo": {"name": "fake"}}

    def call(self, name, arguments):
        self.calls.append((name, arguments))
        if name == evernote_export.T_NOTEBOOKS:
            return {"hits": [{"notebookId": "nb1", "label": "Notebook", "stack": "Stack"}]}
        if name == evernote_export.T_TAGS:
            return {"hits": [
                {"tagId": "tag1", "label": "Root"},
                {"tagId": "tag2", "label": "Child", "parentId": "tag1"},
            ]}
        if name == evernote_export.T_NOTES:
            return {"hits": [{"noteId": "note1", "title": "Hello"}], "isLastPage": True, "totalResultCount": 1}
        if name == evernote_export.T_NOTE:
            return {
                "id": "note1",
                "title": "Hello",
                "content": "body",
                "createdAt": "2024-01-02T03:04:05",
                "updatedAt": "2024-01-02T03:04:05",
                "notebookId": "nb1",
                "tags": [{"id": "tag1", "name": "Root"}, {"id": "tag2", "name": "Child", "parentId": "tag1"}],
                "resources": [{"name": "file.txt", "hash": "abc1234567890", "sizeBytes": 5}],
            }
        if name == "get_attachment":
            return {"uri": "attachment://1"}
        raise AssertionError(name)

    def _send(self, msg):
        return 200, {"content-type": "application/json"}, b'{"result": {"ok": true}}'

    def _rpc(self, method, params=None, notify=False):
        if notify:
            return None
        return {"ok": True}

    def list_tools(self):
        return [{"name": "demo"}]

    def read_resource(self, uri):
        return [{"blob": base64.b64encode(b"hello").decode("ascii")}]


class SecurityAndConfigTests(unittest.TestCase):
    def test_redacts_sensitive_values(self):
        sample = "token=secret-value refresh_token=refresh-value code=abc123"

        redacted = evernote_export.redact_sensitive_text(sample)

        self.assertNotIn("secret-value", redacted)
        self.assertNotIn("refresh-value", redacted)
        self.assertNotIn("abc123", redacted)
        self.assertIn("token", redacted)
        self.assertIn("refresh_token", redacted)

    def test_environment_overrides_config_paths(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            token_path = os.path.join(tmpdir, "token.json")
            output_dir = os.path.join(tmpdir, "export")

            with mock.patch.dict(
                os.environ,
                {
                    "EVERNOTE_MCP_TOKEN_FILE": token_path,
                    "EVERNOTE_EXPORT_DIR": output_dir,
                },
                clear=False,
            ):
                self.assertEqual(evernote_export.get_token_file(), token_path)
                self.assertEqual(evernote_export.get_output_dir(), output_dir)

    def test_do_export_writes_manifest_and_note_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.object(evernote_export, "get_output_dir", return_value=tmpdir), \
                 mock.patch.object(evernote_export, "MCP", FakeMCP), \
                 mock.patch.object(evernote_export, "get_access_token", return_value="token"), \
                 mock.patch.object(evernote_export, "WITH_ATTACHMENTS", True):
                evernote_export.do_export()

            manifest_dir = os.path.join(tmpdir, "_manifest")
            self.assertTrue(os.path.exists(os.path.join(manifest_dir, "notebooks.json")))
            self.assertTrue(os.path.exists(os.path.join(manifest_dir, "tags.json")))
            self.assertTrue(os.path.exists(os.path.join(manifest_dir, "notes-index.json")))
            self.assertTrue(os.path.exists(os.path.join(manifest_dir, "tag-hierarchy.json")))

            note_dir = os.path.join(tmpdir, "Stack", "Notebook")
            note_files = os.listdir(note_dir)
            self.assertTrue(any(name.endswith(".enex") for name in note_files))
            self.assertTrue(any(name.endswith(".json") for name in note_files))
            self.assertTrue(any("_attachments" in name for name in note_files))

    def test_refresh_token_writes_updated_token_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            token_path = os.path.join(tmpdir, "token.json")
            with mock.patch.dict(os.environ, {"EVERNOTE_MCP_TOKEN_FILE": token_path}, clear=False):
                with mock.patch.object(evernote_export, "_http", return_value=(200, {}, json.dumps({"access_token": "new-token", "expires_in": 3600}).encode("utf-8"))):
                    access_token = evernote_export._refresh_token({
                        "refresh_token": "refresh-1",
                        "client_id": "client-1",
                        "token_endpoint": "https://token.example",
                    })

                self.assertEqual(access_token, "new-token")
                with open(token_path, encoding="utf-8") as handle:
                    saved = json.load(handle)
                self.assertEqual(saved["access_token"], "new-token")

    def test_export_helper_functions_and_attachment_download(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.object(evernote_export, "get_output_dir", return_value=tmpdir):
                self.assertEqual(evernote_export._safe("bad/name"), "bad_name")
                self.assertEqual(evernote_export._enex_ts("2024-01-02T03:04:05"), "20240102030405Z")
                self.assertIsNone(evernote_export._enex_ts(None))
                os.makedirs(os.path.join(tmpdir, "_manifest"), exist_ok=True)
                note = {
                    "title": "Hello",
                    "content": "body",
                    "createdAt": "2024-01-02T03:04:05",
                    "updatedAt": "2024-01-02T03:04:05",
                    "tags": [{"name": "Tag"}],
                }
                enex = evernote_export._build_enex(note)
                self.assertIn("<en-export", enex)
                self.assertIn("<tag>Tag</tag>", enex)
                evernote_export._mark_done("note-1")
                self.assertEqual(evernote_export._load_done(), {"note-1"})
                evernote_export._save_tag_nodes({"tag-1": {"name": "Root", "parentId": None}})
                self.assertEqual(evernote_export._load_tag_nodes()["tag-1"]["name"], "Root")
                evernote_export._dump("_manifest/extra.json", {"ok": True})
                self.assertTrue(os.path.exists(os.path.join(tmpdir, "_manifest", "extra.json")))
                self.assertEqual(evernote_export._build_tree({"root": {"name": "Root", "parentId": None}}), [{"name": "Root", "children": []}])

                folder = os.path.join(tmpdir, "folder")
                os.makedirs(folder, exist_ok=True)
                fake_mcp = FakeMCP("token")
                saved = evernote_export._download_attachments(fake_mcp, {"id": "note-1", "resources": [{"name": "f.txt", "hash": "abcdef123456"}]}, folder, "base")
                self.assertEqual(saved, 1)
                self.assertTrue(os.path.exists(os.path.join(folder, "base_attachments")))

    def test_main_respects_no_attachments_flag(self):
        original = evernote_export.WITH_ATTACHMENTS
        try:
            with mock.patch.object(evernote_export, "do_export") as exporter, \
                 mock.patch.object(sys, "argv", ["evernote_export.py", "--no-attachments", "export"]):
                evernote_export.main()
            exporter.assert_called_once_with()
            self.assertFalse(evernote_export.WITH_ATTACHMENTS)
        finally:
            evernote_export.WITH_ATTACHMENTS = original

    def test_http_helpers_and_mcp_retry_paths(self):
        class FakeResponse:
            def __init__(self, status, body, headers=None):
                self.status = status
                self._body = body
                self.headers = headers or {}
            def read(self):
                return self._body

        def fake_urlopen(request, timeout):
            if request.full_url.endswith("/ok"):
                return FakeResponse(200, b'{"ok": true}', {"Content-Type": "application/json"})
            raise urllib.error.HTTPError(request.full_url, 503, "boom", {}, io.BytesIO(b"bad"))

        with mock.patch.object(evernote_export.urllib.request, "urlopen", side_effect=fake_urlopen), \
             mock.patch.object(evernote_export.time, "sleep", return_value=None):
            status, headers, body = evernote_export._http("GET", "https://example.test/ok")
            self.assertEqual(status, 200)
            self.assertEqual(body, b'{"ok": true}')
            self.assertEqual(evernote_export._get_json("https://example.test/ok"), {"ok": True})

        with mock.patch.object(evernote_export, "_get_json", side_effect=[
            {"authorization_servers": ["https://auth.example"]},
            {"authorization_endpoint": "https://auth.example/authorize", "token_endpoint": "https://token.example"},
        ]):
            self.assertEqual(evernote_export.discover_auth_server()["token_endpoint"], "https://token.example")

        with mock.patch.object(evernote_export, "_http", return_value=(201, {}, json.dumps({"client_id": "client-1"}).encode("utf-8"))):
            self.assertEqual(evernote_export.register_client({"registration_endpoint": "https://reg.example"}), "client-1")

        with mock.patch.object(evernote_export, "_http", side_effect=[(429, {"retry-after": "0"}, b""), (200, {}, b"{}")]), \
             mock.patch.object(evernote_export.time, "sleep", return_value=None):
            mcp = evernote_export.MCP("token")
            self.assertEqual(mcp._send({"jsonrpc": "2.0"})[0], 200)

        self.assertEqual(evernote_export.MCP._parse({"content-type": "text/event-stream"}, b'data: {"ok": true}\n'), {"ok": True})

    def test_do_tools_and_login_paths(self):
        with mock.patch.object(evernote_export, "get_access_token", return_value="token"), \
             mock.patch.object(evernote_export, "MCP", FakeMCP):
            evernote_export.do_tools()

        captured = {}

        def fake_http(method, url, *, headers=None, data=None, form=None, json_body=None):
            if method == "POST" and "registration" in url:
                captured["reg"] = True
                return 200, {}, json.dumps({"client_id": "client-1"}).encode("utf-8")
            if method == "POST" and "token" in url:
                captured["token"] = True
                return 200, {}, json.dumps({"access_token": "tok", "expires_in": 3600}).encode("utf-8")
            if method == "GET":
                return 200, {}, json.dumps({"authorization_servers": ["https://auth.example"], "registration_endpoint": "https://reg.example"}).encode("utf-8")
            raise AssertionError(method)

        with mock.patch.object(evernote_export, "discover_auth_server", return_value={"authorization_endpoint": "https://auth.example/authorize", "token_endpoint": "https://token.example"}), \
             mock.patch.object(evernote_export, "register_client", return_value="client-1"), \
             mock.patch.object(evernote_export, "_http", side_effect=fake_http), \
             mock.patch.object(evernote_export.http.server, "HTTPServer") as http_server, \
             mock.patch.object(evernote_export.webbrowser, "open"), \
             mock.patch.object(evernote_export.time, "sleep", return_value=None), \
             mock.patch.object(evernote_export, "get_token_file", return_value=os.path.join(tempfile.gettempdir(), "token.json")):
            http_server.return_value.serve_forever.side_effect = lambda: None
            http_server.return_value.shutdown.side_effect = lambda: None
            state = "state"
            evernote_export._CallbackHandler.captured = {"code": "abc", "state": state}
            with mock.patch.object(evernote_export, "_b64url", side_effect=lambda b: "challenge"):
                with mock.patch.object(evernote_export.secrets, "token_urlsafe", return_value=state):
                    evernote_export.do_login()

    def test_repair_missing_main_writes_note_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = os.path.join(tmpdir, "export")
            os.makedirs(os.path.join(out_dir, "_manifest"), exist_ok=True)
            with open(os.path.join(out_dir, "_manifest", "notebooks.json"), "w", encoding="utf-8") as handle:
                json.dump([{"notebookId": "nb1", "label": "Notebook", "stack": "Stack"}], handle)
            with mock.patch.object(evernote_export, "OUT_DIR", out_dir), \
                 mock.patch.object(evernote_export, "MCP", FakeMCP), \
                 mock.patch.object(evernote_export, "get_access_token", return_value="token"), \
                 mock.patch.object(evernote_export, "get_output_dir", return_value=out_dir):
                repair_missing.main()
            self.assertTrue(os.path.exists(os.path.join(out_dir, "Stack", "Notebook", "Hello-note1.enex")))
            self.assertTrue(os.path.exists(os.path.join(out_dir, "Stack", "Notebook", "Hello-note1.json")))

    def test_cleanup_removes_downloaded_output(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = os.path.join(tmpdir, "export")
            os.makedirs(os.path.join(out_dir, "_manifest"), exist_ok=True)
            os.makedirs(os.path.join(out_dir, "Stack", "Notebook"), exist_ok=True)
            with open(os.path.join(out_dir, "_manifest", "notes-index.json"), "w", encoding="utf-8") as handle:
                handle.write("{}")
            with open(os.path.join(out_dir, "Stack", "Notebook", "note.enex"), "w", encoding="utf-8") as handle:
                handle.write("<en-export/>")

            with mock.patch.object(evernote_export, "get_output_dir", return_value=out_dir):
                evernote_export.do_cleanup()

            self.assertTrue(os.path.exists(out_dir))
            self.assertEqual(os.listdir(out_dir), [])

    def test_verify_export_main_reports_results(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = os.path.join(tmpdir, "export")
            os.makedirs(os.path.join(out_dir, "_manifest"), exist_ok=True)
            os.makedirs(os.path.join(out_dir, "Stack", "Notebook"), exist_ok=True)
            with open(os.path.join(out_dir, "_manifest", "notes-index.json"), "w", encoding="utf-8") as handle:
                json.dump([{"noteId": "note1", "title": "Hello"}], handle)
            with open(os.path.join(out_dir, "_manifest", "notebooks.json"), "w", encoding="utf-8") as handle:
                json.dump([{"notebookId": "nb1", "label": "Notebook"}], handle)
            with open(os.path.join(out_dir, "_manifest", "tags.json"), "w", encoding="utf-8") as handle:
                json.dump([{"tagId": "tag1", "label": "Root"}], handle)
            with open(os.path.join(out_dir, "_manifest", "tag-hierarchy.json"), "w", encoding="utf-8") as handle:
                json.dump({"flat": {"tag1": {"name": "Root", "parentId": None}}}, handle)
            note_path = os.path.join(out_dir, "Stack", "Notebook", "Hello-note1.json")
            resource_hash = hashlib.md5(b"hello").hexdigest()
            with open(note_path, "w", encoding="utf-8") as handle:
                json.dump({"id": "note1", "title": "Hello", "content": "body", "created": "2024-01-02T03:04:05", "resources": [{"hash": resource_hash, "sizeBytes": 5}]}, handle)
            enex_path = os.path.join(out_dir, "Stack", "Notebook", "Hello-note1.enex")
            with open(enex_path, "w", encoding="utf-8") as handle:
                handle.write("<en-export><note><title>Hello</title></note></en-export>")
            attachment_dir = os.path.join(out_dir, "Stack", "Notebook", "Hello-note1_attachments")
            os.makedirs(attachment_dir, exist_ok=True)
            with open(os.path.join(attachment_dir, f"{resource_hash[:8]}_file.txt"), "wb") as handle:
                handle.write(b"hello")
            with open(os.path.join(out_dir, "Stack", "Notebook", "extra.json"), "w", encoding="utf-8") as handle:
                json.dump({"id": "extra", "title": "Extra", "content": "body", "created": "2024-01-02T03:04:05", "resources": []}, handle)
            with mock.patch.object(verify_export, "OUT", out_dir), \
                 mock.patch.object(verify_export, "MAN", os.path.join(out_dir, "_manifest")), \
                 mock.patch.object(evernote_export, "MCP", FakeMCP), \
                 mock.patch.object(evernote_export, "get_access_token", return_value="token"), \
                 mock.patch.object(sys, "argv", ["verify_export.py", "--server"]):
                verify_export.main()

    def test_oauth_and_http_helpers_cover_more_branches(self):
        class FakeResponse:
            def __init__(self, status, body, headers=None):
                self.status = status
                self._body = body
                self.headers = headers or {}

            def read(self):
                return self._body

        def fake_urlopen(req, timeout):
            if req.data == b"raw":
                return FakeResponse(200, b"raw", {"Content-Type": "text/plain"})
            if req.data == urllib.parse.urlencode({"a": "b"}).encode():
                return FakeResponse(200, b"ok", {"Content-Type": "text/plain"})
            if req.data == b'{"a": 1}':
                return FakeResponse(200, b"ok", {"Content-Type": "text/plain"})
            raise AssertionError(req.data)

        with mock.patch.object(evernote_export.urllib.request, "urlopen", side_effect=fake_urlopen):
            self.assertEqual(evernote_export._http("POST", "https://example", data=b"raw")[0], 200)
            self.assertEqual(evernote_export._http("POST", "https://example", form={"a": "b"})[0], 200)
            self.assertEqual(evernote_export._http("POST", "https://example", json_body={"a": 1})[0], 200)

        self.assertEqual(evernote_export._b64url(b"abc"), "YWJj")
        self.assertEqual(evernote_export.redact_sensitive_text(b"token=abc"), "token=<redacted>")
        self.assertEqual(evernote_export._safe_body(b"refresh_token=abc"), "refresh_token=<redacted>")

        with mock.patch.object(evernote_export, "_get_json", side_effect=[
            {"authorization_servers": ["https://auth.example"]},
            SystemExit("first"),
            {"authorization_endpoint": "https://auth.example/authorize"},
        ]):
            self.assertEqual(evernote_export.discover_auth_server()["authorization_endpoint"], "https://auth.example/authorize")

        with mock.patch.object(evernote_export, "_get_json", side_effect=[
            {"authorization_servers": ["https://auth.example"]},
            SystemExit("first"),
            SystemExit("second"),
        ]):
            with self.assertRaises(SystemExit):
                evernote_export.discover_auth_server()

        with mock.patch.object(evernote_export, "_http", return_value=(400, {}, b'{"error": true}')):
            with self.assertRaises(SystemExit):
                evernote_export.register_client({"registration_endpoint": "https://reg.example"})

    def test_login_and_token_helpers_cover_error_paths(self):
        handler = evernote_export._CallbackHandler.__new__(evernote_export._CallbackHandler)
        handler.path = "/callback?code=abc&state=s1"
        handler.send_response = mock.Mock()
        handler.send_header = mock.Mock()
        handler.end_headers = mock.Mock()
        handler.wfile = io.BytesIO()
        evernote_export._CallbackHandler.captured = {}
        evernote_export._CallbackHandler.do_GET(handler)
        self.assertEqual(evernote_export._CallbackHandler.captured["code"], "abc")

        with mock.patch.object(evernote_export, "discover_auth_server", return_value={
            "authorization_endpoint": "https://auth.example/authorize",
            "token_endpoint": "https://token.example",
        }), \
             mock.patch.object(evernote_export, "register_client", return_value="client-1"), \
             mock.patch.object(evernote_export, "_b64url", side_effect=lambda b: "challenge"), \
             mock.patch.object(evernote_export.secrets, "token_urlsafe", return_value="state"), \
             mock.patch.object(evernote_export.http.server, "HTTPServer") as http_server, \
             mock.patch.object(evernote_export.webbrowser, "open"), \
             mock.patch.object(evernote_export.time, "sleep", return_value=None), \
             mock.patch.object(evernote_export, "_http", return_value=(200, {}, json.dumps({"access_token": "tok", "expires_in": 3600}).encode("utf-8"))), \
             mock.patch.object(evernote_export, "get_token_file", return_value=os.path.join(tempfile.gettempdir(), "token.json")):
            http_server.return_value.serve_forever.side_effect = lambda: None
            http_server.return_value.shutdown.side_effect = lambda: None
            evernote_export._CallbackHandler.captured = {"code": "abc", "state": "bad"}
            with self.assertRaises(SystemExit):
                evernote_export.do_login()

        with mock.patch.object(evernote_export.os.path, "exists", return_value=False):
            with self.assertRaises(SystemExit):
                evernote_export.get_access_token()

        with tempfile.TemporaryDirectory() as tmpdir:
            token_path = os.path.join(tmpdir, "token.json")
            with open(token_path, "w", encoding="utf-8") as handle:
                json.dump({"access_token": "old", "expires_at": 0}, handle)
            with mock.patch.object(evernote_export, "get_token_file", return_value=token_path):
                with self.assertRaises(SystemExit):
                    evernote_export.get_access_token()

    def test_mcp_retry_and_rpc_error_paths(self):
        with mock.patch.object(evernote_export, "_http", side_effect=[
            urllib.error.URLError("boom"),
            (200, {}, b"{}"),
        ]), \
             mock.patch.object(evernote_export.time, "sleep", return_value=None):
            mcp = evernote_export.MCP("token")
            self.assertEqual(mcp._send({"jsonrpc": "2.0"})[0], 200)

        with mock.patch.object(evernote_export, "_http", side_effect=[
            (401, {}, b"token expired"),
            (200, {}, b"{}"),
        ]), \
             mock.patch.object(evernote_export, "refresh_access_token", return_value="new-token"), \
             mock.patch.object(evernote_export.time, "sleep", return_value=None):
            mcp = evernote_export.MCP("token")
            self.assertEqual(mcp._send({"jsonrpc": "2.0"})[0], 200)
            self.assertEqual(mcp.token, "new-token")

        mcp = evernote_export.MCP("token")
        mcp._send = mock.Mock(return_value=(200, {}, b'{"error": "boom"}'))
        with self.assertRaises(SystemExit):
            mcp._rpc("demo")

        mcp._send = mock.Mock(return_value=(200, {"mcp-session-id": "sid"}, b'{"result": {"ok": true}}'))
        self.assertEqual(mcp._rpc("demo"), {"ok": True})
        self.assertEqual(mcp.session_id, "sid")

    def test_do_export_uses_cached_index_and_handles_note_errors(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = os.path.join(tmpdir, "export")
            os.makedirs(os.path.join(out_dir, "_manifest"), exist_ok=True)
            with open(os.path.join(out_dir, "_manifest", "notes-index.json"), "w", encoding="utf-8") as handle:
                json.dump([{"noteId": "note1", "title": "Hello"}], handle)

            class CachedFakeMCP(FakeMCP):
                def call(self, name, arguments):
                    self.calls.append((name, arguments))
                    if name == evernote_export.T_TAGS:
                        raise SystemExit("tags unavailable")
                    if name == evernote_export.T_NOTE:
                        return "bad-payload"
                    return super().call(name, arguments)

            with mock.patch.object(evernote_export, "get_output_dir", return_value=out_dir), \
                 mock.patch.object(evernote_export, "MCP", CachedFakeMCP), \
                 mock.patch.object(evernote_export, "get_access_token", return_value="token"), \
                 mock.patch.object(evernote_export, "WITH_ATTACHMENTS", False):
                evernote_export.do_export()

            self.assertTrue(os.path.exists(os.path.join(out_dir, "_manifest", "tag-hierarchy.json")))


if __name__ == "__main__":
    unittest.main()
