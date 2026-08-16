"""Tests for verify.py - reconciling proposals.json / PROPOSALS.md / GitHub / ledger."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import services.verify as vf


def write_json(path, data):
    path.write_text(json.dumps(data))
    return path


def make_comment(cid, login, created="2026-08-16T10:00:00Z", body="hi"):
    return {"id": cid, "user": {"login": login}, "created_at": created, "body": body}


class VerifyTestBase(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.root = Path(self.td.name)
        self.secrets = self.root / "secrets.json"
        write_json(self.secrets, {"GITHUB_TOKEN": "tok"})

    def build(self, proposals, proposals_md, ledger_entries, rejected=None):
        cfg = {"proposals": proposals, "rejected_candidates": rejected or []}
        cfg_path = self.root / "services/proposals.json"
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        write_json(cfg_path, cfg)
        md_path = self.root / "deliverables/PROPOSALS.md"
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(proposals_md)
        ledger_path = self.root / "treasury/ledger/ledger.jsonl"
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        with open(ledger_path, "w") as fh:
            for e in ledger_entries:
                fh.write(json.dumps(e) + "\n")

    def run_verify(self, gh_get=None, has_token=True):
        secrets_path = str(self.secrets) if has_token else str(self.root / "nope.json")
        patchers = [
            mock.patch.object(sys, "argv", ["verify.py", "--root", str(self.root),
                                            "--secrets", secrets_path]),
        ]
        if gh_get is not None:
            patchers.append(mock.patch.object(vf, "gh_get", side_effect=gh_get))
        for p in patchers:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patchers])
        # main() uses args defaulting to relative paths under --root
        return vf.main()


class TestVerifyConsistent(unittest.TestCase):
    def test_all_consistent_returns_0(self):
        proposal = {"repo": "getAlby/bitcoin-connect", "issue": 159, "title": "Docs"}
        link = "https://github.com/getAlby/bitcoin-connect/issues/159"
        md = f"# Proposals\n\n| x | status |\n|---|\n| [D]({link}) | awaiting reply |"
        ledger = [{"event": "bounty_attempt", "repo": "getAlby/bitcoin-connect",
                   "issue": 159, "outcome": "negotiating", "sats": 0}]
        w = VerifyTestBase()
        w.setUp()
        try:
            w.build([proposal], md, ledger)
            rc = w.run_verify(gh_get=lambda url, token: (
                {"state": "open"} if "comments" not in url else
                [make_comment(100, "MejorQueNada")]))
            self.assertEqual(rc, 0)
        finally:
            w.tearDown()


class TestVerifyDiscrepancies(unittest.TestCase):
    def test_reply_after_ours_not_in_md_is_discrepancy(self):
        """The exact fabrication case: GitHub has a reply after ours but PROPOSALS.md
        still says awaiting reply -> must exit non-zero."""
        proposal = {"repo": "getAlby/bitcoin-connect", "issue": 159, "title": "Docs"}
        link = "https://github.com/getAlby/bitcoin-connect/issues/159"
        md = f"# Proposals\n\n| [D]({link}) | awaiting reply |"
        ledger = [{"event": "bounty_attempt", "repo": "getAlby/bitcoin-connect",
                   "issue": 159, "outcome": "negotiating", "sats": 0}]
        w = VerifyTestBase()
        w.setUp()
        try:
            w.build([proposal], md, ledger)
            def gh(url, token):
                if "comments" not in url:
                    return {"state": "open"}
                return [make_comment(100, "MejorQueNada", created="2026-08-16T05:00:00Z"),
                        make_comment(200, "rolznz", created="2026-08-16T12:00:00Z")]
            rc = w.run_verify(gh_get=gh)
            self.assertEqual(rc, 1)
        finally:
            w.tearDown()

    def test_missing_ledger_entry_is_discrepancy(self):
        proposal = {"repo": "a/b", "issue": 1, "title": "T"}
        link = "https://github.com/a/b/issues/1"
        w = VerifyTestBase()
        w.setUp()
        try:
            w.build([proposal], f"[T]({link}) | awaiting reply |", [])
            rc = w.run_verify(gh_get=lambda url, token: (
                {"state": "open"} if "comments" not in url else
                [make_comment(50, "MejorQueNada")]))
            self.assertEqual(rc, 1)
        finally:
            w.tearDown()

    def test_closed_issue_is_discrepancy(self):
        proposal = {"repo": "a/b", "issue": 2, "title": "T2"}
        link = "https://github.com/a/b/issues/2"
        w = VerifyTestBase()
        w.setUp()
        try:
            w.build([proposal], f"[T2]({link}) | awaiting reply |",
                    [{"event": "bounty_attempt", "repo": "a/b", "issue": 2,
                      "outcome": "negotiating"}])
            rc = w.run_verify(gh_get=lambda url, token: (
                {"state": "closed"} if "comments" not in url else
                [make_comment(50, "MejorQueNada")]))
            self.assertEqual(rc, 1)
        finally:
            w.tearDown()

    def test_rejected_candidate_with_contact_is_discrepancy(self):
        w = VerifyTestBase()
        w.setUp()
        try:
            w.build([], "no proposals",
                    [{"event": "bounty_attempt", "repo": "a/b", "issue": 3,
                      "outcome": "negotiating"}],
                    rejected=[{"repo": "a/b", "issue": 3, "reason": "contested"}])
            rc = w.run_verify(gh_get=lambda url, token: (
                {"state": "open"} if "comments" not in url else
                [make_comment(50, "MejorQueNada")]))
            self.assertEqual(rc, 1)
        finally:
            w.tearDown()


class TestVerifyJson(unittest.TestCase):
    def test_json_output_mode(self):
        proposal = {"repo": "a/b", "issue": 1, "title": "T"}
        link = "https://github.com/a/b/issues/1"
        w = VerifyTestBase()
        w.setUp()
        try:
            w.build([proposal], f"[T]({link}) | awaiting reply |",
                    [{"event": "bounty_attempt", "repo": "a/b", "issue": 1,
                      "outcome": "negotiating"}])
            from io import StringIO
            buf = StringIO()
            with mock.patch.object(sys, "stdout", buf), \
                 mock.patch.object(sys, "argv", ["verify.py", "--root", str(w.root), "--json"]), \
                 mock.patch.object(vf.os.path, "expanduser", return_value=str(w.secrets)), \
                 mock.patch.object(vf, "gh_get", side_effect=lambda url, token: (
                     {"state": "open"} if "comments" not in url else
                     [make_comment(50, "MejorQueNada")])):
                rc = vf.main()
            self.assertEqual(rc, 0)
            out = json.loads(buf.getvalue())
            self.assertTrue(out["ok"])
            self.assertEqual(out["proposals"], 1)
        finally:
            w.tearDown()

    def test_no_token_flagged_in_json(self):
        proposal = {"repo": "a/b", "issue": 1, "title": "T"}
        link = "https://github.com/a/b/issues/1"
        w = VerifyTestBase()
        w.setUp()
        try:
            w.build([proposal], f"[T]({link}) | awaiting reply |",
                    [{"event": "bounty_attempt", "repo": "a/b", "issue": 1,
                      "outcome": "negotiating"}])
            from io import StringIO
            buf = StringIO()
            with mock.patch.object(sys, "stdout", buf), \
                 mock.patch.object(sys, "argv", ["verify.py", "--root", str(w.root), "--json"]), \
                 mock.patch.object(vf.os.path, "expanduser", return_value=str(w.root / "nope.json")):
                rc = vf.main()
            out = json.loads(buf.getvalue())
            self.assertFalse(out["ok"])
            self.assertTrue(any("GITHUB_TOKEN" in d for d in out["discrepancies"]))
        finally:
            w.tearDown()


if __name__ == "__main__":
    unittest.main()
