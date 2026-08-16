#!/usr/bin/env python3
"""Bounty Desk - verify: reconcile the desk's records against live reality.

Checks four views of the same facts and flags any discrepancy:
  1. proposals.json    - the in-flight proposal list (source of truth for scope)
  2. PROPOSALS.md      - the watcher's last rendered status digest
  3. live GitHub       - issue state + comments (the ground truth)
  4. treasury/ledger   - bounty_attempt entries (what we actually logged)

Exits non-zero (and prints a machine-readable summary) when a discrepancy is
found.

Usage:
  verify.py [--root /path/to/repo] [--json]
            [--config services/proposals.json]
            [--proposals-md deliverables/PROPOSALS.md]
            [--ledger treasury/ledger/ledger.jsonl]
            [--secrets ~/.config/bounty-agent/secrets.json]
            [--owner your-github-username]
"""

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

OWNER_ACCOUNT = os.environ.get("BOUNTY_OWNER_ACCOUNT", "MejorQueNada")


def gh_get(url, token):
    req = urllib.request.Request(url, headers={
        "User-Agent": "bounty-agent-verify/0.1",
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def load_json(path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception as exc:
        return {"__parse_error__": str(exc)}


def check(issues, ok, msg):
    issues.append((ok, msg))
    if not ok:
        print(f"[verify] DISCREPANCY: {msg}", file=sys.stderr)


def check_ok(issues, msg):
    issues.append((True, msg))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default=".")
    ap.add_argument("--json", action="store_true", help="emit JSON to stdout")
    ap.add_argument("--config", default="services/proposals.json")
    ap.add_argument("--proposals-md", default="deliverables/PROPOSALS.md")
    ap.add_argument("--ledger", default="treasury/ledger/ledger.jsonl")
    ap.add_argument("--secrets",
                    default=os.path.expanduser("~/.config/bounty-agent/secrets.json"),
                    help="JSON file with GITHUB_TOKEN")
    ap.add_argument("--owner", default=OWNER_ACCOUNT,
                    help="your GitHub username (comments from this account are treated as ours)")
    args = ap.parse_args()

    root = Path(args.root)
    owner = args.owner
    issues = []

    token = ""
    try:
        token = json.loads(Path(args.secrets).read_text()).get("GITHUB_TOKEN", "")
    except Exception:
        token = ""

    config = load_json(root / args.config)
    proposals_md = (root / args.proposals_md).read_text() if (root / args.proposals_md).exists() else ""
    ledger = load_json_lines(root / args.ledger) if (root / args.ledger).exists() else []

    if config is None:
        print("[verify] config not found; nothing to verify", file=sys.stderr)
        return 2
    if isinstance(config, dict) and config.get("__parse_error__"):
        check(issues, False, f"proposals.json unparseable: {config['__parse_error__']}")
        config = {"proposals": [], "rejected_candidates": []}
    proposals = config.get("proposals", [])

    # --- 1. proposals.json vs ledger ---------------------------------------
    ledger_entries = [e for e in ledger
                      if isinstance(e, dict) and e.get("event") == "bounty_attempt"]
    for p in proposals:
        repo, issue = p["repo"], p["issue"]
        matches = [e for e in ledger_entries
                   if e.get("repo") == repo and e.get("issue") == issue]
        if not matches:
            check(issues, False, f"{repo}#{issue} in proposals.json but NO "
                                 f"bounty_attempt entry in ledger")
        else:
            if not any(e.get("outcome") == "negotiating" for e in matches):
                check(issues, False, f"{repo}#{issue} ledger entries exist but none "
                                     f"marked outcome=negotiating")
            else:
                check_ok(issues, f"{repo}#{issue} ledger ok (outcome=negotiating)")

    # rejected_candidates should NOT have a contact in ledger (outcome not-contacted ok)
    for rc in config.get("rejected_candidates", []):
        repo, issue = rc.get("repo", ""), rc.get("issue")
        if not repo:
            continue
        contacted = [e for e in ledger_entries
                     if e.get("repo") == repo and e.get("issue") == issue
                     and e.get("outcome") == "negotiating"]
        if contacted:
            check(issues, False, f"{repo}#{issue} listed as rejected but ledger has "
                                 f"outcome=negotiating entry")
        else:
            check_ok(issues, f"{repo}#{issue} rejected — no contact in ledger")

    # --- 2. proposals.json vs PROPOSALS.md ---------------------------------
    for p in proposals:
        repo, issue = p["repo"], p["issue"]
        link = f"https://github.com/{repo}/issues/{issue}"
        if link not in proposals_md:
            check(issues, False, f"{repo}#{issue} in proposals.json but missing from "
                                 f"PROPOSALS.md ({link} not found)")
        else:
            check_ok(issues, f"{repo}#{issue} present in PROPOSALS.md")

    # --- 3. live GitHub ground truth ---------------------------------------
    if not token:
        check(issues, False, "no GITHUB_TOKEN — live GitHub checks skipped")
        github = {}
    else:
        github = {}
        for p in proposals:
            repo, issue = p["repo"], p["issue"]
            try:
                iss = gh_get(f"https://api.github.com/repos/{repo}/issues/{issue}", token)
                comments = gh_get(
                    f"https://api.github.com/repos/{repo}/issues/{issue}/comments?per_page=100", token)
            except Exception as exc:
                check(issues, False, f"{repo}#{issue} GitHub fetch failed: {exc}")
                continue
            ours = [c for c in comments if (c.get("user") or {}).get("login") == owner]
            non_ours_after = [
                c for c in comments
                if (c.get("user") or {}).get("login") != owner
                and ours and c["created_at"] > ours[-1]["created_at"]
            ]
            state = iss.get("state")
            github[f"{repo}#{issue}"] = {
                "issue_state": state,
                "num_comments": len(comments),
                "our_comment": bool(ours),
                "reply_after_ours": bool(non_ours_after),
                "reply_by": [(c.get("user") or {}).get("login") for c in non_ours_after],
            }
            if state != "open":
                check(issues, False, f"{repo}#{issue} is {state} on GitHub but still "
                                     f"tracked as in-flight")
            else:
                check_ok(issues, f"{repo}#{issue} open on GitHub")
            if not ours:
                check(issues, False, f"{repo}#{issue} tracked as in-flight but no "
                                     f"{owner} comment exists on it")
            else:
                check_ok(issues, f"{repo}#{issue} our contact comment verified on GitHub")

            # status string in PROPOSALS.md must match reality
            if non_ours_after:
                if "NEW REPLY" not in proposals_md:
                    check(issues, False, f"{repo}#{issue}: GitHub shows a reply after ours "
                                         f"but PROPOSALS.md does not mark NEW REPLY")
                else:
                    check_ok(issues, f"{repo}#{issue} NEW REPLY reflected in PROPOSALS.md")
            else:
                check_ok(issues, f"{repo}#{issue} awaiting reply (verified live)")

    # --- ledger entries that reference proposals not in config ---------------
    for e in ledger_entries:
        repo, issue = e.get("repo", ""), e.get("issue", 0)
        if not repo or issue == 0:
            continue
        if e.get("outcome") == "abandoned" or e.get("outcome") == "not-contacted":
            continue
        in_config = any(p["repo"] == repo and p["issue"] == issue for p in proposals)
        if not in_config:
            check(issues, False, f"ledger has negotiating entry {repo}#{issue} but it is "
                                 f"not in proposals.json")
    else:
        check_ok(issues, "all ledger negotiating entries appear in proposals.json")

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    summary = {
        "checked_at": stamp,
        "proposals": len(proposals),
        "discrepancies": [msg for ok, msg in issues if not ok],
        "ok": not any(not ok for ok, _ in issues),
        "github": github,
    }

    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(f"[verify] {stamp} — {len(proposals)} proposals checked, "
              f"{len(summary['discrepancies'])} discrepancy(ies)")
        for ok, msg in issues:
            print(("  OK  " if ok else "  !!  ") + msg)
        if not issues:
            print("  (no checks ran)")

    return 0 if summary["ok"] else 1


def load_json_lines(path):
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            out.append({"__parse_error__": line[:80]})
    return out


if __name__ == "__main__":
    sys.exit(main())