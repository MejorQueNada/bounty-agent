"""Tests for scout.py — scoring, filtering, caching, first-mover detection, markdown.

Run: python3 -m unittest discover -s tests -v   (from repo root)
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import services.scout as scout


def tmpdir():
    return tempfile.TemporaryDirectory()


class TestFitScore(unittest.TestCase):
    def test_reward_plus_unlocked_plus_lang_fit(self):
        item = {"reward_sats": 10000, "unlocked_sats": 10000, "language": "python"}
        score, fit = scout.fit_score(item, {"python", "typescript"})
        self.assertTrue(fit)
        self.assertEqual(score, 12000)  # 10000 * 1.2 (lang) * 1.0 (unlocked)

    def test_locked_funds_penalized(self):
        item = {"reward_sats": 10000, "unlocked_sats": 0, "language": "python"}
        score, fit = scout.fit_score(item, {"python"})
        self.assertEqual(score, 7200)  # 10000 * 0.6 (locked) * 1.2

    def test_lang_miss_gets_no_bonus(self):
        item = {"reward_sats": 10000, "unlocked_sats": 10000, "language": "rust"}
        score, fit = scout.fit_score(item, {"python", "typescript"})
        self.assertFalse(fit)
        self.assertEqual(score, 10000)


class TestStackerAward(unittest.TestCase):
    def test_plain_difficulty(self):
        labels = [{"name": "difficulty:easy"}]
        self.assertEqual(scout.stacker_award(labels), 100_000)

    def test_priority_multiplier(self):
        labels = [{"name": "difficulty:medium"}, {"name": "priority:urgent"}]
        self.assertEqual(scout.stacker_award(labels), 250_000 * 3)

    def test_no_difficulty_returns_none(self):
        self.assertIsNone(scout.stacker_award([{"name": "priority:high"}]))


class TestRateRewarder(unittest.TestCase):
    def test_low_trust_new_account_no_followers(self):
        gh = mock.Mock()
        gh.gh.return_value = {
            "created_at": "2026-05-01T00:00:00Z",
            "followers": 0,
            "public_repos": 1,
            "public_gists": 0,
        }
        self.assertEqual(scout.rate_rewarder(gh, "newbie"), "low-trust")

    def test_established_account_ok(self):
        gh = mock.Mock()
        gh.gh.return_value = {
            "created_at": "2019-03-01T00:00:00Z",
            "followers": 200,
            "public_repos": 40,
            "public_gists": 5,
        }
        self.assertEqual(scout.rate_rewarder(gh, "alice"), "ok")

    def test_empty_username_unknown(self):
        self.assertEqual(scout.rate_rewarder(mock.Mock(), ""), "unknown")


class TestMarkNew(unittest.TestCase):
    def test_first_mover_detection(self):
        with tmpdir() as td:
            cache = Path(td)
            items = [
                {"repo": "a/b", "issue_number": 1},
                {"repo": "a/b", "issue_number": 2},
            ]
            n = scout.mark_new(items, cache)
            self.assertEqual(n, 2)
            self.assertTrue(all(i["is_new"] for i in items))
            # second run: nothing new
            items2 = [{"repo": "a/b", "issue_number": 1}]
            n2 = scout.mark_new(items2, cache)
            self.assertEqual(n2, 0)
            self.assertFalse(items2[0]["is_new"])


class TestGhCache(unittest.TestCase):
    def test_get_put_roundtrip(self):
        with tmpdir() as td:
            c = scout.GhCache(Path(td))
            c.put("k", {"v": 1})
            self.assertEqual(c.get("k"), {"v": 1})
            # file persisted
            c2 = scout.GhCache(Path(td))
            self.assertEqual(c2.get("k"), {"v": 1})

    def test_ttl_expiry(self):
        with tmpdir() as td:
            c = scout.GhCache(Path(td))
            c.put("k", {"v": 1})
            with mock.patch.object(scout.time, "time", return_value=scout.time.time() + 1000):
                self.assertIsNone(c.get("k", ttl=10))

    def test_fresh_wipes_cache(self):
        with tmpdir() as td:
            c = scout.GhCache(Path(td))
            c.put("k", {"v": 1})
            c2 = scout.GhCache(Path(td), fresh=True)
            self.assertIsNone(c2.get("k"))

    def test_gh_uses_token_and_sleeps(self):
        with tmpdir() as td:
            c = scout.GhCache(Path(td), token="TOKEN")
            with mock.patch("services.scout.urllib.request.urlopen") as urlopen, \
                 mock.patch("services.scout.time.sleep") as sleep:
                resp = mock.Mock()
                resp.__enter__ = mock.Mock(return_value=resp)
                resp.__exit__ = mock.Mock(return_value=False)
                resp.read.return_value = json.dumps({"ok": True}).encode()
                urlopen.return_value = resp
                self.assertEqual(c.gh("https://api.github.com/x"), {"ok": True})
                req = urlopen.call_args[0][0]
                self.assertEqual(req.get_header("Authorization"), "Bearer TOKEN")
                sleep.assert_called_once()
                # second call is cached, no new request
                c.gh("https://api.github.com/x")
                self.assertEqual(urlopen.call_count, 1)


class TestEnrichLb(unittest.TestCase):
    LB_OPEN = {
        "is_closed": False,
        "winner_data": None,
        "unexpired_total_rewards": 5000,
        "unlocked_total_rewards": 5000,
        "issue_number": 42,
        "title": "Fix bug",
        "html_url": "https://github.com/a/b/issues/42",
        "repository_data": {"full_name": "a/b"},
        "last_rewarder_data": {"github_username": "alice"},
        "created_at": "2026-08-01T00:00:00Z",
        "modified_at": "2026-08-02T00:00:00Z",
        "body": "some body",
    }

    def gh(self, data):
        gh = mock.Mock()
        gh.gh.side_effect = data  # keyed by URL via get() actually; simpler: lambda
        return gh

    def test_skips_closed_and_won(self):
        for field, val in (("is_closed", True), ("winner_data", {"x": 1})):
            it = dict(self.LB_OPEN)
            it[field] = val
            gh = mock.Mock()
            out = scout.enrich_lb(gh, [it], 2000)
            self.assertEqual(out, [])

    def test_skips_below_min_reward(self):
        it = dict(self.LB_OPEN)
        it["unexpired_total_rewards"] = 1000
        gh = mock.Mock()
        out = scout.enrich_lb(gh, [it], 2000)
        self.assertEqual(out, [])

    def test_skips_closed_on_github(self):
        gh = mock.Mock()
        gh.gh.return_value = {"language": "python", "archived": False}
        # gh_issue_state hits cache then gh.gh
        with mock.patch.object(scout, "gh_issue_state", return_value="closed"):
            out = scout.enrich_lb(gh, [self.LB_OPEN], 2000)
        self.assertEqual(out, [])

    def test_enriches_open_issue(self):
        gh = mock.Mock()
        with mock.patch.object(scout, "gh_issue_state", return_value="open"), \
             mock.patch.object(scout, "gh_repo_archived", return_value=False), \
             mock.patch.object(scout, "find_open_prs", return_value=[]), \
             mock.patch.object(scout, "language_of", return_value=("python", "github")), \
             mock.patch.object(scout, "rate_rewarder", return_value="ok"):
            out = scout.enrich_lb(gh, [self.LB_OPEN], 2000)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["platform"], "lightning-bounties")
        self.assertEqual(out[0]["reward_sats"], 5000)
        self.assertEqual(out[0]["contested"], False)
        self.assertEqual(out[0]["repo"], "a/b")


class TestEnrichAlby(unittest.TestCase):
    def test_skips_archived_repo(self):
        gh = mock.Mock()
        with mock.patch.object(scout, "gh_repo_archived", return_value=True):
            out = scout.enrich_alby(gh, [{
                "repository_url": "https://api.github.com/repos/getAlby/x",
                "number": 5,
                "title": "T",
                "html_url": "u",
                "state": "open",
                "created_at": "2026-08-01T00:00:00Z",
                "updated_at": "2026-08-01T00:00:00Z",
                "body": "b",
            }])
        self.assertEqual(out, [])

    def test_marks_negotiated_and_contested(self):
        gh = mock.Mock()
        with mock.patch.object(scout, "gh_repo_archived", return_value=False), \
             mock.patch.object(scout, "language_of", return_value=("typescript", "known")), \
             mock.patch.object(scout, "find_open_prs", return_value=[{"number": 900}]):
            out = scout.enrich_alby(gh, [{
                "repository_url": "https://api.github.com/repos/getAlby/x",
                "number": 5,
                "title": "T",
                "html_url": "u",
                "state": "open",
                "created_at": "2026-08-01T00:00:00Z",
                "updated_at": "2026-08-01T00:00:00Z",
                "body": "b",
            }])
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0]["reward_negotiated"])
        self.assertEqual(out[0]["reward_sats"], 0)
        self.assertTrue(out[0]["contested"])


class TestLanguageOf(unittest.TestCase):
    def test_known_hint(self):
        gh = mock.Mock()
        lang, src = scout.language_of(gh, "getAlby/bitcoin-connect")
        self.assertEqual((lang, src), ("javascript", "known"))
        gh.gh.assert_not_called()

    def test_github_lookup(self):
        gh = mock.Mock()
        gh.gh.return_value = {"language": "Python"}
        lang, src = scout.language_of(gh, "unknown/repo")
        self.assertEqual((lang, src), ("python", "github"))


class TestRenderMarkdown(unittest.TestCase):
    def test_header_and_rows(self):
        items = [{
            "repo": "a/b", "issue_number": 1, "title": "Fix it", "url": "u",
            "language": "python", "reward_sats": 5000, "unlocked_sats": 5000,
            "rewarder_trust": "ok", "open_prs": [], "contested": False,
            "reward_negotiated": False, "is_new": True, "fit": True,
        }]
        md = scout.render_markdown(items, {"python"}, {
            "lb_total": 10, "lb_open_eligible": 1, "lb_archived": 0,
            "stacker_difficulty_open": 0, "alby_open": 0, "contested": 0,
            "total_open_unexpired": 5000,
        })
        self.assertIn("# Bounty Scout", md)
        self.assertIn("| 5,000 | yes | NEW |  | a/b#1 | python | YES | ok | Fix it", md)
        self.assertIn("Preferred languages: python", md)

    def test_negotiated_item_labeled(self):
        items = [{
            "repo": "g/bc", "issue_number": 2, "title": "Docs", "url": "u",
            "language": "javascript", "reward_sats": 0, "unlocked_sats": 0,
            "rewarder_trust": "ok", "open_prs": [], "contested": False,
            "reward_negotiated": True, "is_new": False, "fit": True,
        }]
        md = scout.render_markdown(items, {"javascript"}, {
            "lb_total": 0, "lb_open_eligible": 0, "lb_archived": 0,
            "stacker_difficulty_open": 0, "alby_open": 1, "contested": 0,
            "total_open_unexpired": 0,
        })
        self.assertIn("negotiated", md)
        self.assertIn("Docs [negotiated]", md)

    def test_empty_list(self):
        md = scout.render_markdown([], set(), {
            "lb_total": 0, "lb_open_eligible": 0, "lb_archived": 0,
            "stacker_difficulty_open": 0, "alby_open": 0, "contested": 0,
            "total_open_unexpired": 0,
        })
        self.assertIn("No eligible candidates found", md)


if __name__ == "__main__":
    unittest.main()