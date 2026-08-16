"""Tests for watch_proposals.py — reply detection, seeding, PROPOSALS.md rendering.

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
import services.watch_proposals as wp


def make_comment(cid, login, created="2026-08-16T10:00:00Z", body="hello"):
    return {"id": cid, "user": {"login": login}, "created_at": created, "body": body}


def write_json(path, data):
    path.write_text(json.dumps(data))
    return str(path)


class WatchTestBase(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.cache = Path(self.td.name) / "cache"
        self.outdir = Path(self.td.name) / "deliverables"
        self.cache.mkdir()
        self.secrets = Path(self.td.name) / "secrets.json"
        write_json(self.secrets, {"GITHUB_TOKEN": "test-token"})
        self.cfg = write_json(Path(self.td.name) / "proposals.json",
                              {"proposals": [{"repo": "getAlby/bc", "issue": 159,
                                              "title": "Docs site"}]})

    def run_watch(self, gh_get):
        with mock.patch.object(wp.os.path, "expanduser", return_value=str(self.secrets)), \
             mock.patch.object(wp, "gh_get", side_effect=gh_get):
            old_argv = sys.argv
            sys.argv = ["watch_proposals.py", "--cache-dir", str(self.cache),
                        "--outdir", str(self.outdir), "--config", self.cfg]
            try:
                return wp.main()
            finally:
                sys.argv = old_argv

    def state(self):
        return json.loads((self.cache / "proposal_watch.json").read_text())


class TestWatchSeeding(unittest.TestCase):
    def test_first_run_seeds_from_our_latest_comment(self):
        """First run sets watch position to our own latest comment id.
        Thread history before ours is never re-flagged; a reply after ours
        is surfaced on the NEXT run (it's genuinely new to us)."""
        comments = [make_comment(10, "rolznz", created="2026-08-15T00:00:00Z"),
                    make_comment(100, "MejorQueNada", created="2026-08-16T05:00:00Z")]
        w = WatchTestBase()
        w.setUp()
        try:
            url = "https://api.github.com/repos/getAlby/bc/issues/159/comments?per_page=100"
            rc = w.run_watch(lambda url_, token: comments if url_ == url else [])
            self.assertEqual(rc, 0)
            self.assertEqual(w.state()["getAlby/bc#159"]["last_comment_id"], 100)
            md = (w.outdir / "PROPOSALS.md").read_text()
            self.assertIn("watching (seeded)", md)
        finally:
            w.tearDown()

    def test_no_retro_alert_for_old_thread(self):
        """Old maintainer comments (before ours) are never flagged as NEW REPLY."""
        comments = [make_comment(10, "rolznz", created="2024-03-04T00:00:00Z"),
                    make_comment(20, "vr-varad", created="2024-03-05T00:00:00Z"),
                    make_comment(100, "MejorQueNada", created="2026-08-16T05:00:00Z")]
        w = WatchTestBase()
        w.setUp()
        try:
            url = "https://api.github.com/repos/getAlby/bc/issues/159/comments?per_page=100"
            rc = w.run_watch(lambda url_, token: comments if url_ == url else [])
            self.assertEqual(rc, 0)
            md = (w.outdir / "PROPOSALS.md").read_text()
            self.assertIn("watching (seeded)", md)
            self.assertNotIn("NEW REPLY", md)
        finally:
            w.tearDown()


class TestWatchReplyDetection(unittest.TestCase):
    def test_new_reply_after_ours_is_flagged(self):
        """A maintainer comment AFTER our latest is surfaced as NEW REPLY."""
        url = "https://api.github.com/repos/getAlby/bc/issues/159/comments?per_page=100"
        w = WatchTestBase()
        w.setUp()
        try:
            # run 1: seed from our comment (id 100)
            w.run_watch(lambda url_, token: [make_comment(100, "MejorQueNada")])
            # run 2: maintainer replied after us → NEW REPLY
            rc = w.run_watch(lambda url_, token: [
                make_comment(100, "MejorQueNada", created="2026-08-16T05:00:00Z"),
                make_comment(200, "rolznz", created="2026-08-16T12:00:00Z", body="ok looks good"),
            ])
            self.assertEqual(rc, 0)
            md = (w.outdir / "PROPOSALS.md").read_text()
            self.assertIn("NEW REPLY", md)
            self.assertIn("from rolznz", md)
        finally:
            w.tearDown()

    def test_own_comment_never_counts_as_reply(self):
        """Comments we post ourselves never surface as NEW REPLY."""
        url = "https://api.github.com/repos/getAlby/bc/issues/159/comments?per_page=100"
        w = WatchTestBase()
        w.setUp()
        try:
            w.run_watch(lambda url_, token: [make_comment(100, "MejorQueNada")])
            rc = w.run_watch(lambda url_, token: [
                make_comment(100, "MejorQueNada"),
                make_comment(150, "MejorQueNada"),  # we posted again
            ])
            md = (w.outdir / "PROPOSALS.md").read_text()
            self.assertIn("awaiting reply", md)
            self.assertNotIn("NEW REPLY", md)
            self.assertEqual(rc, 0)
        finally:
            w.tearDown()

    def test_waiting_status_on_no_change(self):
        url = "https://api.github.com/repos/getAlby/bc/issues/159/comments?per_page=100"
        w = WatchTestBase()
        w.setUp()
        try:
            w.run_watch(lambda url_, token: [make_comment(100, "MejorQueNada")])
            rc = w.run_watch(lambda url_, token: [make_comment(100, "MejorQueNada")])
            md = (w.outdir / "PROPOSALS.md").read_text()
            self.assertIn("awaiting reply", md)
            self.assertEqual(rc, 0)
        finally:
            w.tearDown()


class TestWatchFailures(unittest.TestCase):
    def test_fetch_failure_reported_not_fatal(self):
        w = WatchTestBase()
        w.setUp()
        try:
            rc = w.run_watch(lambda url, token: (_ for _ in ()).throw(RuntimeError("boom")))
            self.assertEqual(rc, 0)
            md = (w.outdir / "PROPOSALS.md").read_text()
            self.assertIn("check failed", md)
        finally:
            w.tearDown()

    def test_no_token_returns_1(self):
        w = WatchTestBase()
        w.setUp()
        try:
            write_json(w.secrets, {})
            rc = w.run_watch(lambda url, token: [])
            self.assertEqual(rc, 1)
        finally:
            w.tearDown()


if __name__ == "__main__":
    unittest.main()