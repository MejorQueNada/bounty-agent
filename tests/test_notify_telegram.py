"""Tests for notify_telegram.py — dedupe, chunking, HTML escaping, push behavior.

Run: python3 -m unittest discover -s tests -v   (from the bounty-desk repo root)
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import services.notify_telegram as nt


class TestFingerprint(unittest.TestCase):
    def test_deterministic(self):
        a = nt.fingerprint("same text")
        b = nt.fingerprint("same text")
        self.assertEqual(a, b)

    def test_differs_on_content(self):
        self.assertNotEqual(nt.fingerprint("x"), nt.fingerprint("y"))


class TestEscapeHtml(unittest.TestCase):
    def test_escapes_angle_brackets_and_amp(self):
        self.assertEqual(nt.escape_html("a < b & c > d"),
                         "a &lt; b &amp; c &gt; d")


class TestChunkText(unittest.TestCase):
    def test_short_text_single_chunk(self):
        self.assertEqual(nt.chunk_text("hello world"), ["hello world"])

    def test_long_text_split_on_lines(self):
        text = "\n".join("line %d - %s" % (i, "x" * 100) for i in range(60))
        chunks = nt.chunk_text(text, limit=2000)
        self.assertGreater(len(chunks), 1)
        for c in chunks:
            self.assertLessEqual(len(c), 2000)


class TestPushSource(unittest.TestCase):
    def test_unchanged_content_not_repushed(self):
        with tempfile.TemporaryDirectory() as td:
            outdir = Path(td)
            (outdir / "ALERTS.md").write_text("# Fresh bounty alert\nstuff")
            state = {}
            sent = []
            with mock.patch.object(nt, "send_message", side_effect=lambda tok, chat, txt: sent.append(txt)):
                nt.push_source("t", "c", outdir / "ALERTS.md", "Fresh bounty alerts", state)
            self.assertEqual(len(sent), 1)
            self.assertIn("Fresh bounty alerts", sent[0])
            # second call: fingerprint unchanged → nothing sent
            sent.clear()
            with mock.patch.object(nt, "send_message", side_effect=lambda tok, chat, txt: sent.append(txt)):
                nt.push_source("t", "c", outdir / "ALERTS.md", "Fresh bounty alerts", state)
            self.assertEqual(sent, [])

    def test_changed_content_repushed(self):
        with tempfile.TemporaryDirectory() as td:
            outdir = Path(td)
            f = outdir / "ALERTS.md"
            f.write_text("v1")
            state = {}
            sent = []
            with mock.patch.object(nt, "send_message", side_effect=lambda tok, chat, txt: sent.append(txt)):
                nt.push_source("t", "c", f, "Fresh bounty alerts", state)
            f.write_text("v2")
            with mock.patch.object(nt, "send_message", side_effect=lambda tok, chat, txt: sent.append(txt)):
                nt.push_source("t", "c", f, "Fresh bounty alerts", state)
            self.assertEqual(len(sent), 2)

    def test_missing_file_noop(self):
        with tempfile.TemporaryDirectory() as td:
            outdir = Path(td)
            state = {}
            sent = []
            with mock.patch.object(nt, "send_message", side_effect=lambda tok, chat, txt: sent.append(txt)):
                nt.push_source("t", "c", outdir / "NOPE.md", "X", state)
            self.assertEqual(sent, [])

    def test_empty_content_sets_state_no_send(self):
        with tempfile.TemporaryDirectory() as td:
            outdir = Path(td)
            f = outdir / "A.md"
            f.write_text("   \n  ")
            state = {}
            sent = []
            with mock.patch.object(nt, "send_message", side_effect=lambda tok, chat, txt: sent.append(txt)):
                nt.push_source("t", "c", f, "A", state)
            self.assertEqual(sent, [])
            self.assertEqual(len(state), 1)


class TestSendMessage(unittest.TestCase):
    def test_calls_telegram_api(self):
        with mock.patch.object(nt.urllib.request, "urlopen") as urlopen:
            resp = mock.Mock()
            resp.__enter__ = mock.Mock(return_value=resp)
            resp.__exit__ = mock.Mock(return_value=False)
            resp.read.return_value = b"{}"
            urlopen.return_value = resp
            nt.send_message("TOKEN", "12345", "hello <b>world</b>")
            req = urlopen.call_args[0][0]
            self.assertIn("api.telegram.org/botTOKEN/sendMessage", req.full_url)
            body = req.data.decode()
            self.assertIn("chat_id=12345", body)
            self.assertIn("hello+%3Cb%3Eworld%3C%2Fb%3E", body)
            self.assertIn("parse_mode=HTML", body)

    def test_chunks_long_message_multiple_calls(self):
        with mock.patch.object(nt.urllib.request, "urlopen") as urlopen:
            resp = mock.Mock()
            resp.__enter__ = mock.Mock(return_value=resp)
            resp.__exit__ = mock.Mock(return_value=False)
            resp.read.return_value = b"{}"
            urlopen.return_value = resp
            nt.send_message("T", "1", "\n".join("x" * 100 for _ in range(60)))
            self.assertGreater(urlopen.call_count, 1)


class TestLoadSecrets(unittest.TestCase):
    def test_missing_token_exits(self):
        with tempfile.TemporaryDirectory() as td:
            secrets = Path(td) / "secrets.json"
            secrets.write_text(json.dumps({"TELEGRAM_CHAT_ID": "1"}))
            with mock.patch.object(nt, "SECRETS_PATH", secrets):
                with self.assertRaises(SystemExit):
                    nt.load_secrets()

    def test_missing_file_exits(self):
        with mock.patch.object(nt, "SECRETS_PATH", Path("/nonexistent/secrets.json")):
            with self.assertRaises(SystemExit):
                nt.load_secrets()

    def test_returns_token_and_chat(self):
        with tempfile.TemporaryDirectory() as td:
            secrets = Path(td) / "secrets.json"
            secrets.write_text(json.dumps({"TELEGRAM_BOT_TOKEN": "abc", "TELEGRAM_CHAT_ID": "42"}))
            with mock.patch.object(nt, "SECRETS_PATH", secrets):
                self.assertEqual(nt.load_secrets(), ("abc", "42"))


class TestMain(unittest.TestCase):
    def test_test_mode_sends_test_message(self):
        with mock.patch.object(nt, "load_secrets", return_value=("tok", "42")), \
             mock.patch.object(nt, "send_message") as send, \
             mock.patch.object(sys, "argv", ["notify_telegram.py", "--test"]):
            nt.main()
        send.assert_called_once()
        self.assertIn("Test message", send.call_args[0][2])

    def test_regular_run_pushes_changed_sources(self):
        with tempfile.TemporaryDirectory() as td:
            outdir = Path(td) / "deliverables"
            outdir.mkdir()
            (outdir / "ALERTS.md").write_text("NEW BOUNTY X")
            (outdir / "PROPOSALS.md").write_text("proposal table")
            state_file = Path(td) / "notify_state.json"
            with mock.patch.object(nt, "load_secrets", return_value=("tok", "42")), \
                 mock.patch.object(nt, "send_message") as send, \
                 mock.patch.object(sys, "argv", ["notify_telegram.py",
                                                 "--outdir", str(outdir),
                                                 "--state", str(state_file)]):
                nt.main()
            self.assertEqual(send.call_count, 2)
            state = json.loads(state_file.read_text())
            self.assertEqual(len(state), 2)


if __name__ == "__main__":
    unittest.main()