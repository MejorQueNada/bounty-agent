#!/usr/bin/env python3
"""Open-source bounty scout service.

Polls sats-paying open-source bounty sources, normalizes them, scores them for
fit with your skills (Python/JS/TS + static-analysis driven work), flags risk,
and emits a ranked digest for an agent or cron log.

Sources:
  Lightning Bounties  - public feed: GET https://app.lightningbounties.com/api/issues/
  Stacker News repo   - GitHub issues tagged difficulty:* / priority:* (awards
                        paid in sats by stackernews for merged PRs)
  Alby bounty program - getAlby org issues labeled "good first issue" (Alby
                        pays negotiated bounties for them on request; see
                        guides.getalby.com/developer-guide/bounties)

No authentication required for the LB feed. GitHub API calls use the
GITHUB_TOKEN from the secrets file when present (cached on disk with
per-key TTLs to stay well inside rate limits).

Usage:
  scout.py [--min-reward 2000] [--langs python,javascript,typescript]
           [--cache-dir ~/.cache/bounty-agent] [--outdir deliverables]
           [--secrets ~/.config/bounty-agent/secrets.json]
           [--fresh] [--debug]
"""

import argparse
import datetime as dt
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

GITHUB_STATE_CACHE_TTL = 6 * 3600
PR_SEARCH_CACHE_TTL = 6 * 3600

LB_FEED = "https://app.lightningbounties.com/api/issues/"
LB_PAGE = 100
STACKER_REPO = "stackernews/stacker.news"
DIFFICULTY_BASE = {
    "difficulty:good-first-issue": 20_000,
    "difficulty:easy": 100_000,
    "difficulty:medium": 250_000,
    "difficulty:medium-hard": 500_000,
    "difficulty:hard": 1_000_000,
}
PRIORITY_MULT = {
    "priority:low": 0.5,
    "priority:medium": 1.5,
    "priority:high": 2.0,
    "priority:urgent": 3.0,
}
DEFAULT_CACHE_TTL = 24 * 3600
STACKER_CACHE_TTL = 6 * 3600
ALBY_CACHE_TTL = 6 * 3600
# getAlby marks bounty-eligible work with the "good first issue" label; payouts
# are negotiated per issue (no escrow) — see ALBY_BOUNTY_DOC.
ALBY_GOOD_FIRST_SEARCH = (
    "https://api.github.com/search/issues"
    "?q=org:getAlby%20is:issue%20is:open%20label:%22good%20first%20issue%22"
    "&per_page=100&sort=updated&order=desc"
)
ALBY_BOUNTY_DOC = "https://guides.getalby.com/developer-guide/bounties"

# Static language hints for repos we already know about (avoids GitHub calls).
REPO_LANG_HINT = {
    "stackernews/stacker.news": "javascript",
    "lnbits/lnbits": "python",
    "BrantaOps/branta-core": "typescript",
    "BrantaOps/core": "typescript",
    "BrantaOps/bugs": "typescript",
    "PrimalHQ/primal-web-app": "typescript",
    "PrimalHQ/primal-ios-app": "swift",
    "PrimalHQ/primal-android-app": "kotlin",
    "CodyTseng/jumble": "kotlin",
    "gitroomhq/postiz-app": "typescript",
    "cryptoadvance/specter-diy": "python",
    "MagnivOrg/prompt-layer-library": "python",
    "chainwayxyz/clementine": "rust",
    "Lightning-Bounties/lb-next": "typescript",
    "getAlby/hub": "typescript",
    "getAlby/lightning-browser-extension": "javascript",
    "getAlby/bitcoin-connect": "javascript",
    "getAlby/alby-js-sdk": "typescript",
    "getAlby/nwc-js-sdk": "typescript",
    "getAlby/nostr-wallet-connect": "typescript",
    "getAlby/webln": "typescript",
    "getAlby/pos": "typescript",
    "getAlby/ZapPlanner": "typescript",
    "getAlby/paidmcp": "typescript",
    "getAlby/mcp": "typescript",
    "getAlby/hub-skill": "typescript",
    "getAlby/builder-skill": "typescript",
    "getAlby/payments-skill": "typescript",
    "getAlby/alby-companion-rs": "rust",
    "getAlby/alby-installer-linux": "rust",
    "getAlby/ldn-rs": "rust",
}


def http_json(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "bounty-agent-scout/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


class GhCache:
    """Minimal disk cache for GitHub API responses with per-key TTL."""

    def __init__(self, cache_dir: Path, ttl=DEFAULT_CACHE_TTL, token="", fresh=False):
        self.path = cache_dir / "gh_cache.json"
        self.ttl = ttl
        self.token = token
        self.data = {}
        if fresh:
            try:
                self.path.unlink()
            except OSError:
                pass
        elif self.path.exists():
            try:
                self.data = json.loads(self.path.read_text())
            except Exception:
                self.data = {}

    def get(self, key, ttl=None):
        ttl = ttl or self.ttl
        hit = self.data.get(key)
        if not hit:
            return None
        age = time.time() - hit.get("ts", 0)
        if age > ttl:
            return None
        return hit["value"]

    def put(self, key, value):
        self.data[key] = {"ts": time.time(), "value": value}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data))
        tmp.replace(self.path)

    def gh(self, url, ttl=None):
        cached = self.get(url, ttl)
        if cached is not None:
            return cached
        headers = {"User-Agent": "bounty-agent-scout/0.1"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        self.put(url, data)
        time.sleep(0.6)
        return data


def fetch_lb():
    """Paginate the public Lightning Bounties feed."""
    issues = []
    skip = 0
    while True:
        url = f"{LB_FEED}?distinct_issues=true&skip={skip}&limit={LB_PAGE}"
        page = http_json(url)
        if not page:
            break
        issues.extend(page)
        if len(page) < LB_PAGE:
            break
        skip += LB_PAGE
    return issues


def gh_repo_info(gh, full_name):
    return gh.gh(f"https://api.github.com/repos/{full_name}")


def gh_user_info(gh, username):
    return gh.gh(f"https://api.github.com/users/{username}")


def gh_issue_state(gh, repo, number, token):
    """Live GitHub open/closed state for an issue. Handles repo renames."""
    key = f"gh:issue:{repo}#{number}"
    cached = gh.get(key, GITHUB_STATE_CACHE_TTL)
    if cached is not None:
        return cached
    url = f"https://api.github.com/repos/{repo}/issues/{number}"
    try:
        data = gh.gh(url, GITHUB_STATE_CACHE_TTL)
        state = data.get("state") if isinstance(data, dict) else None
        if isinstance(data, dict) and data.get("message") == "Moved Permanently":
            state = "moved"
        if state is None:
            state = "unknown"
    except Exception:
        state = "unknown"
    gh.put(key, state)
    return state


def gh_repo_archived(gh, full_name):
    """Whether a repo is archived (read-only → PRs can never merge)."""
    key = f"gh:repo-archived:{full_name}"
    cached = gh.get(key)
    if cached is not None:
        return cached
    try:
        r = gh_repo_info(gh, full_name)
        archived = bool(r.get("archived")) if isinstance(r, dict) else False
    except Exception:
        archived = False
    gh.put(key, archived)
    return archived


def find_open_prs(gh, repo, issue_number):
    """Open PRs in `repo` that reference the issue (contest detection).

    A bounty whose issue already has open PRs is contested: the platform pays
    only the first merged PR. Search is advisory (any mention of the issue
    number in an open PR counts) and cached per issue.
    """
    key = f"gh:prs:{repo}#{issue_number}"
    cached = gh.get(key, PR_SEARCH_CACHE_TTL)
    if cached is not None:
        return cached
    q = urllib.parse.quote(f"repo:{repo} is:pr is:open {issue_number}")
    url = f"https://api.github.com/search/issues?q={q}&per_page=20"
    try:
        d = gh.gh(url, PR_SEARCH_CACHE_TTL)
        items = [
            {"number": i["number"], "title": i.get("title", "")[:80],
             "user": (i.get("user") or {}).get("login", "?")}
            for i in (d.get("items") or [])
        ]
    except Exception:
        items = []
    gh.put(key, items)
    return items


def rate_rewarder(gh, username):
    """Heuristic trust flag for the person who funded a bounty."""
    if not username:
        return "unknown"
    try:
        u = gh_user_info(gh, username)
    except Exception:
        return "unknown"
    if isinstance(u, dict) and u.get("message"):
        return "unknown"
    created = u.get("created_at", "")
    followers = u.get("followers") or 0
    repos = (u.get("public_repos") or 0) + (u.get("public_gists") or 0)
    try:
        age_days = (dt.date.today() - dt.date.fromisoformat(created[:10])).days
    except Exception:
        age_days = 9999
    if followers == 0 and age_days < 180 and repos < 3:
        return "low-trust"
    return "ok"


def language_of(gh, full_name):
    hint = REPO_LANG_HINT.get(full_name)
    if hint:
        return hint, "known"
    try:
        r = gh_repo_info(gh, full_name)
        lang = r.get("language") if isinstance(r, dict) else None
        return (lang.lower() if lang else "unknown"), "github"
    except Exception:
        return "unknown", "error"


def fetch_stacker(gh):
    """Open issues in the Stacker News repo carrying difficulty/priority labels."""
    cached = gh.get("stacker:issues", STACKER_CACHE_TTL)
    if cached is not None:
        return cached
    found = []
    for label in DIFFICULTY_BASE:
        url = (
            "https://api.github.com/repos/stackernews/stacker.news/issues"
            f"?state=open&labels={urllib.parse.quote(label)}&per_page=100"
        )
        try:
            page = gh.gh(url, STACKER_CACHE_TTL)
        except Exception:
            continue
        if isinstance(page, list):
            found.extend(page)
    # dedupe by number
    seen = {}
    for i in found:
        seen[i["number"]] = i
    gh.put("stacker:issues", list(seen.values()))
    return list(seen.values())


def stacker_award(labels):
    difficulty = [l["name"] for l in labels if l["name"] in DIFFICULTY_BASE]
    if not difficulty:
        return None
    base = DIFFICULTY_BASE[difficulty[0]]
    mult = 1.0
    for l in labels:
        if l["name"] in PRIORITY_MULT:
            mult = max(mult, PRIORITY_MULT[l["name"]])
    return int(base * mult)


def fetch_alby(gh):
    """Open getAlby issues labeled 'good first issue' (bounty-eligible on request).

    Unlike LB/Stacker these are NOT escrowed: Alby negotiates a payout per issue
    (contact-first, concept-first for complex ones). reward_sats stays 0 and the
    entry is flagged reward_negotiated.
    """
    cached = gh.get("alby:good-first-issues", ALBY_CACHE_TTL)
    if cached is not None:
        return cached
    d = gh.gh(ALBY_GOOD_FIRST_SEARCH, ALBY_CACHE_TTL)
    items = [i for i in (d.get("items") or []) if i.get("state") == "open"]
    gh.put("alby:good-first-issues", items)
    return items


def enrich_alby(gh, issues):
    out = []
    for it in issues:
        repo = it.get("repository_url", "").rsplit("/repos/", 1)[-1] or "getAlby/unknown"
        if gh_repo_archived(gh, repo):
            continue
        lang, src = language_of(gh, repo)
        open_prs = find_open_prs(gh, repo, it.get("number"))
        out.append({
            "platform": "alby-program",
            "title": it.get("title", "").strip(),
            "repo": repo,
            "issue_number": it.get("number"),
            "url": it.get("html_url"),
            "language": lang,
            "lang_source": src,
            "reward_sats": 0,
            "unlocked_sats": 0,
            "reward_negotiated": True,
            "rewarder": "getalby",
            "rewarder_trust": "ok",
            "gh_state": "open",
            "repo_archived": False,
            "open_prs": open_prs,
            "contested": bool(open_prs),
            "created": (it.get("created_at") or "")[:10],
            "modified": (it.get("updated_at") or "")[:10],
            "body_excerpt": (it.get("body") or "").replace("\r", "")[:240],
        })
    return out


def enrich_lb(gh, issues, min_reward, token=None):
    out = []
    for it in issues:
        if it.get("is_closed") or it.get("winner_data"):
            continue
        reward = it.get("unexpired_total_rewards") or 0
        if reward < min_reward:
            continue
        repo = (it.get("repository_data") or {}).get("full_name") or "unknown/repo"
        lang, src = language_of(gh, repo)
        gh_state = gh_issue_state(gh, repo, it.get("issue_number"), token)
        if gh_state not in ("open", "unknown"):
            continue
        archived = gh_repo_archived(gh, repo)
        open_prs = find_open_prs(gh, repo, it.get("issue_number")) if not archived else []
        rewarder = ((it.get("last_rewarder_data") or {}).get("github_username")) or ""
        trust = rate_rewarder(gh, rewarder) if rewarder else "unknown"
        unlocked = it.get("unlocked_total_rewards") or 0
        out.append({
            "platform": "lightning-bounties",
            "title": it.get("title", "").strip(),
            "repo": repo,
            "issue_number": it.get("issue_number"),
            "url": it.get("html_url"),
            "language": lang,
            "lang_source": src,
            "reward_sats": reward,
            "unlocked_sats": unlocked,
            "rewarder": rewarder,
            "rewarder_trust": trust,
            "gh_state": gh_state,
            "repo_archived": archived,
            "open_prs": open_prs,
            "contested": bool(open_prs),
            "created": it.get("created_at", "")[:10],
            "modified": it.get("modified_at", "")[:10],
            "body_excerpt": (it.get("body") or "").replace("\r", "")[:240],
        })
    return out


def fit_score(item, pref_langs):
    langs = {item["language"]}
    fit = langs & pref_langs
    score = item["reward_sats"]
    if item["unlocked_sats"] > 0:
        score *= 1.0
    else:
        score *= 0.6
    if fit:
        score *= 1.2
    return int(score), bool(fit)


def main():
    ap = argparse.ArgumentParser(description="Bounty desk scout")
    ap.add_argument("--min-reward", type=int, default=2000)
    ap.add_argument("--langs", default="python,javascript,typescript")
    ap.add_argument("--cache-dir", default=os.path.expanduser("~/.cache/bounty-agent"))
    ap.add_argument("--outdir", default="deliverables")
    ap.add_argument("--fresh", action="store_true")
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--secrets",
                    default=os.path.expanduser("~/.config/bounty-agent/secrets.json"),
                    help="JSON file with GITHUB_TOKEN")
    ap.add_argument("--token", default="", help="GitHub token (or read from the secrets file)")
    args = ap.parse_args()

    pref_langs = {s.strip().lower() for s in args.langs.split(",") if s.strip()}
    token = args.token
    if not token:
        try:
            token = json.loads(Path(args.secrets).read_text()).get("GITHUB_TOKEN", "")
        except Exception:
            token = ""
    cache = GhCache(Path(args.cache_dir), token=token, fresh=args.fresh)
    gh = cache

    # --- Lightning Bounties
    try:
        lb = fetch_lb()
    except Exception as e:
        lb = []
        if args.debug:
            print(f"[warn] LB feed failed: {e}", file=sys.stderr)
    lb_enriched = enrich_lb(gh, lb, args.min_reward, token)

    # --- Stacker News repo (label-based awards)
    try:
        st = fetch_stacker(gh)
    except Exception as e:
        st = []
        if args.debug:
            print(f"[warn] Stacker fetch failed: {e}", file=sys.stderr)
    stacker = []
    for it in st:
        award = stacker_award(it.get("labels") or [])
        if not award or award < args.min_reward:
            continue
        open_prs = find_open_prs(gh, STACKER_REPO, it.get("number"))
        stacker.append({
            "platform": "stacker-repo",
            "title": it.get("title", "").strip(),
            "repo": STACKER_REPO,
            "issue_number": it.get("number"),
            "url": it.get("html_url"),
            "language": REPO_LANG_HINT.get(STACKER_REPO, "javascript"),
            "lang_source": "known",
            "reward_sats": award,
            "unlocked_sats": award,
            "rewarder": "stackernews",
            "rewarder_trust": "ok",
            "gh_state": "open",
            "repo_archived": False,
            "open_prs": open_prs,
            "contested": bool(open_prs),
            "created": (it.get("created_at") or "")[:10],
            "modified": (it.get("updated_at") or "")[:10],
            "body_excerpt": (it.get("body") or "").replace("\r", "")[:240],
        })

    # --- Alby bounty program (getAlby 'good first issue' — negotiated)
    try:
        alby_raw = fetch_alby(gh)
    except Exception as e:
        alby_raw = []
        if args.debug:
            print(f"[warn] Alby fetch failed: {e}", file=sys.stderr)
    alby_enriched = enrich_alby(gh, alby_raw)
    alby_count = len(alby_enriched)
    alby_archived = len(alby_raw) - alby_count
    alby_contested = sum(1 for i in alby_enriched if i.get("contested"))

    archived_count = sum(1 for i in lb_enriched if i.get("repo_archived"))
    lb_enriched = [i for i in lb_enriched if not i.get("repo_archived")]
    items = lb_enriched + stacker + alby_enriched
    contested_count = sum(1 for i in items if i.get("contested"))
    for it in items:
        score, fit = fit_score(it, pref_langs)
        it["fit"] = fit
        it["score"] = score

    items.sort(key=lambda x: (x["unlocked_sats"] > 0, x["score"]), reverse=True)

    new_count = mark_new(items, Path(args.cache_dir))

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    today = dt.date.today().isoformat()
    json_path = outdir / f"scout_{today}.json"
    md_path = outdir / f"scout_{today}.md"
    latest = outdir / "latest.md"

    json_path.write_text(json.dumps(items, indent=2))
    latest_md = render_markdown(items, pref_langs, {
        "lb_total": len(lb),
        "lb_open_eligible": len(lb_enriched),
        "lb_archived": archived_count,
        "stacker_difficulty_open": len(st),
        "alby_open": alby_count,
        "contested": contested_count,
        "total_open_unexpired": sum(i["reward_sats"] for i in items),
    })
    md_path.write_text(latest_md)
    latest.write_text(latest_md)

    if new_count:
        alerts = Path(args.outdir) / "ALERTS.md"
        new_items = [i for i in items if i.get("is_new")]
        lines = [
            f"# 🆕 Fresh bounty alert — {dt.datetime.now(dt.timezone.utc).isoformat()}",
            "",
            f"{new_count} new eligible bounty(s) since last scout run:",
            "",
        ]
        for i in sorted(new_items, key=lambda x: -x["score"]):
            reward = "negotiated" if i.get("reward_negotiated") else f"{i['reward_sats']:,} sats"
            lines.append(f"- **{reward}** · {i['repo']}#{i['issue_number']} · "
                         f"{i['language']} · {i['title']} — {i['url']}")
        alerts.write_text("\n".join(lines) + "\n")
        print(f"🆕 {new_count} NEW bounty(s) — see {alerts}")

    print(f"LB feed: {len(lb)} issues, {len(lb_enriched)} open & eligible (>= {args.min_reward} sats)"
          + (f", {archived_count} on archived repos skipped" if archived_count else ""))
    print(f"Stacker repo: {len(st)} difficulty-labeled issues open (currently {len(stacker)} eligible)")
    print(f"Alby program: {alby_count} open 'good first issue' (negotiated)"
          + (f", {alby_archived} skipped (archived)" if alby_archived else "")
          + (f", {alby_contested} contested" if alby_contested else ""))
    if contested_count:
        print(f"⚠ {contested_count} candidate(s) contested (open PRs already reference them)")
    print(f"Combined escrowed open unexpired: {sum(i['reward_sats'] for i in items)} sats")
    print(f"Wrote {json_path} and {md_path}")
    print("\nTop picks (unlocked first, then score):")
    for i in items[:8]:
        flags = []
        if i["rewarder_trust"] == "low-trust":
            flags.append("LOW-TRUST REWARDER")
        if not i["fit"]:
            flags.append(f"lang={i['language']}")
        if i.get("contested"):
            flags.append(f"contested: {len(i['open_prs'])} open PR(s)")
        if i.get("reward_negotiated"):
            reward_cell = "negot."
        else:
            reward_cell = f"{i['reward_sats']:,}"
        print(f"  {reward_cell:>7} {'✓' if i['unlocked_sats']>0 else '🔒'} "
              f"{i['repo']}#{i['issue_number']} | {i['title'][:55]}")
        if flags:
            print(f"          ⚠ {'; '.join(flags)}")


def load_seen(cache_dir: Path):
    p = cache_dir / "lb_seen.json"
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            return {}
    return {}


def mark_new(items, cache_dir: Path):
    """Flag items never seen before (first-mover detection) and persist the seen set."""
    seen = load_seen(cache_dir)
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    new_keys = []
    for it in items:
        key = f"{it['repo']}#{it['issue_number']}"
        it["is_new"] = key not in seen
        if it["is_new"]:
            new_keys.append(key)
    for key in new_keys:
        seen[key] = now
    (cache_dir / "lb_seen.json").write_text(json.dumps(seen, indent=2))
    return len(new_keys)


def render_markdown(items, pref_langs, stats):
    lines = [
        f"# Bounty Scout — {dt.date.today().isoformat()}",
        "",
        f"LB feed: **{stats['lb_total']}** issues · **{stats['lb_open_eligible']}** open & eligible "
        f"(>= min reward)"
        + (f" · **{stats['lb_archived']}** skipped (archived repos)" if stats.get("lb_archived") else "")
        + (f" · **{stats['contested']}** contested (open PRs exist)" if stats.get("contested") else "")
        + f" · Stacker repo: **{stats['stacker_difficulty_open']}** difficulty-labeled open "
        f"issues · Alby program: **{stats['alby_open']}** open 'good first issue' (negotiated) "
        f"· combined escrowed **{stats['total_open_unexpired']:,} sats**.",
        "",
        "Preferred languages: " + ", ".join(sorted(pref_langs)),
        "",
        "`contest` column = open PRs already referencing the issue; platform pays only the first merged PR.",
        "",
        "`negotiated` = no escrow; payout agreed with the rewarder (Alby program: contact-first, "
        f"see {ALBY_BOUNTY_DOC}).",
        "",
        "## Ranked candidates",
        "",
        "| sats | unlock | new | contest | repo#issue | lang | fit | trust | title |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for i in items:
        trust = "LOW-TRUST" if i["rewarder_trust"] == "low-trust" else i["rewarder_trust"]
        contest = f"{len(i.get('open_prs') or [])}PRs" if i.get("contested") else ""
        reward = "negotiated" if i.get("reward_negotiated") else f"{i['reward_sats']:,}"
        title = i["title"] + " [negotiated]" if i.get("reward_negotiated") else i["title"]
        lines.append(
            f"| {reward} | {'yes' if i['unlocked_sats'] > 0 else 'no'} "
            f"| {'NEW' if i.get('is_new') else ''} | {contest} | {i['repo']}#{i['issue_number']} | {i['language']} "
            f"| {'YES' if i['fit'] else 'no'} | {trust} | {title}"
        )
    lines += ["", "## Full JSON", "", "See `scout_<date>.json` for full bodies and metadata.", ""]
    if not items:
        lines = [
            f"# Bounty Scout — {dt.date.today().isoformat()}",
            "",
            "No eligible candidates found above the reward floor. See JSON for the raw feed.",
            "",
        ]
    return "\n".join(lines)


if __name__ == "__main__":
    main()
