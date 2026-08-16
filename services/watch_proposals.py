#!/usr/bin/env python3
"""Watch in-flight bounty proposals for maintainer replies.

Polls the issues listed in `proposals.json`, tracks the latest comment per
issue in a local cache, and writes a status digest to `PROPOSALS.md`.
Prints a prominent line whenever a NEW reply (not from our own account) is
detected, so cron logs + an agent surface it.

First run seeds the watch position from our own latest comment, so existing
thread history is never re-flagged.

Usage:
  watch_proposals.py [--cache-dir ~/.cache/bounty-agent]
                     [--outdir deliverables]
                     [--config services/proposals.json]
                     [--secrets ~/.config/bounty-agent/secrets.json]
                     [--owner your-github-username]
"""

import argparse
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

OWNER_ACCOUNT = os.environ.get("BOUNTY_OWNER_ACCOUNT", "MejorQueNada")


def gh_get(url, token):
    req = urllib.request.Request(url, headers={
        "User-Agent": "bounty-agent-watch/0.1",
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def load_state(path):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return {}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cache-dir", default=os.path.expanduser("~/.cache/bounty-agent"))
    ap.add_argument("--outdir", default="deliverables")
    ap.add_argument("--config", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "proposals.json"))
    ap.add_argument("--secrets",
                    default=os.path.expanduser("~/.config/bounty-agent/secrets.json"),
                    help="JSON file with GITHUB_TOKEN")
    ap.add_argument("--owner", default=OWNER_ACCOUNT,
                    help="your GitHub username (comments from this account are ignored as replies)")
    args = ap.parse_args()

    owner = args.owner
    token = ""
    try:
        token = json.loads(Path(args.secrets).read_text()).get("GITHUB_TOKEN", "")
    except Exception:
        pass
    if not token:
        print("[watch] no GITHUB_TOKEN — cannot check for replies", file=sys.stderr)
        return 1

    config = json.loads(Path(args.config).read_text())
    cache_dir = Path(args.cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    state = load_state(cache_dir / "proposal_watch.json")

    rows = []
    new_replies = []
    for p in config["proposals"]:
        repo, issue = p["repo"], p["issue"]
        key = f"{repo}#{issue}"
        last_id = state.get(key, {}).get("last_comment_id")
        try:
            comments = gh_get(
                f"https://api.github.com/repos/{repo}/issues/{issue}/comments?per_page=100", token)
        except Exception as exc:
            rows.append((p, "check failed", exc, None))
            print(f"[watch] {key}: fetch failed: {exc}", file=sys.stderr)
            continue
        latest_id = comments[-1]["id"] if comments else None

        if last_id is None:
            ours = [c["id"] for c in comments if (c.get("user") or {}).get("login") == owner]
            last_id = max(ours, default=latest_id or 0)
            state[key] = {"last_comment_id": last_id}
            rows.append((p, "seeded", comments[-1] if comments else None, []))
            continue

        fresh = [c for c in comments if c["id"] > last_id and (c.get("user") or {}).get("login") != owner]
        if latest_id is not None:
            state[key] = {"last_comment_id": latest_id}
        last = comments[-1] if comments else None
        if fresh:
            last_comment = fresh[-1]
            rows.append((p, "reply", last_comment, fresh))
            new_replies.append((p, fresh))
        else:
            rows.append((p, "waiting", last, []))

    (cache_dir / "proposal_watch.json").write_text(json.dumps(state, indent=2))

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "# In-flight proposals (proposal watcher)",
        "",
        f"Checked {stamp} — Alby program, negotiated lane. **No code until the "
        "maintainer confirms scope.**",
        "",
        "| proposal | status | last activity |",
        "|---|---|---|",
    ]
    for p, status, last, _fresh in rows:
        repo, issue = p["repo"], p["issue"]
        title = p.get("title", f"#{issue}")
        if status == "reply":
            who = (last.get("user") or {}).get("login", "?")
            when = (last.get("created_at") or "?")[:10]
            status_cell = f"**NEW REPLY** from {who} ({when})"
        elif status == "waiting":
            status_cell = "awaiting reply"
        elif status == "seeded":
            status_cell = "watching (seeded)"
        else:
            status_cell = f"check failed: {last}"
        last_id = last.get("id") if isinstance(last, dict) else ""
        lines.append(f"| [{title}](https://github.com/{repo}/issues/{issue}) "
                     f"({repo}#{issue}) | {status_cell} | "
                     f"[comment](https://github.com/{repo}/issues/{issue}#issuecomment-{last_id}) |")
    lines.append("")
    (outdir / "PROPOSALS.md").write_text("\n".join(lines) + "\n")

    for p, fresh in new_replies:
        last = fresh[-1]
        who = (last.get("user") or {}).get("login", "?")
        body = (last.get("body") or "").replace("\r", "")[:600]
        print("\n" + "=" * 60)
        print(f"🔔 NEW REPLY on {p['repo']}#{p['issue']} from {who}")
        print(f"   https://github.com/{p['repo']}/issues/{p['issue']}")
        print(body)
        print("=" * 60)
    if new_replies:
        print(f"[watch] {len(new_replies)} proposal(s) with new replies → {outdir}/PROPOSALS.md")
    else:
        print(f"[watch] no new proposal replies ({len(rows)} tracked) → {outdir}/PROPOSALS.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
