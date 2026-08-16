#!/usr/bin/env python3
"""Push new bounty + proposal-reply alerts to the owner via Telegram.

Diffs deliverables/ALERTS.md (scout) and deliverables/PROPOSALS.md
(watcher) against last-sent fingerprints, and pushes new content as
Telegram messages through the Bot API.

The bot is created by the owner via @BotFather; its token and the owner's
chat id live in the secrets file. Only infrastructure / operator code.
"""
import argparse
import datetime as _dt
import hashlib
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

SECRETS_PATH = Path(os.path.expanduser(
    os.environ.get("BOUNTY_SECRETS_PATH", "~/.config/bounty-agent/secrets.json")))
API = "https://api.telegram.org/bot{token}/{method}"


def load_secrets():
    if not SECRETS_PATH.exists():
        sys.exit("[notify] missing secrets file %s" % SECRETS_PATH)
    with open(SECRETS_PATH) as fh:
        data = json.load(fh)
    token = data.get("TELEGRAM_BOT_TOKEN")
    chat_id = data.get("TELEGRAM_CHAT_ID")
    if not token:
        sys.exit("[notify] TELEGRAM_BOT_TOKEN not in secrets")
    if not chat_id:
        sys.exit("[notify] TELEGRAM_CHAT_ID not in secrets (send /start to your bot first)")
    return token, str(chat_id)


def fingerprint(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_state(path):
    if path.exists():
        return json.loads(path.read_text())
    return {}


def escape_html(text):
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def send_message(token, chat_id, text):
    for chunk in chunk_text(text):
        body = urllib.parse.urlencode(
            {"chat_id": chat_id, "text": chunk, "parse_mode": "HTML", "disable_web_page_preview": "true"}
        ).encode()
        req = urllib.request.Request(
            API.format(token=token, method="sendMessage"),
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()


def chunk_text(text, limit=3800):
    if len(text) <= limit:
        return [text]
    chunks, cur = [], ""
    for line in text.splitlines():
        if cur and len(cur) + len(line) + 1 > limit:
            chunks.append(cur)
            cur = ""
        cur = (cur + "\n" if cur else "") + line
    if cur:
        chunks.append(cur)
    return chunks


def push_source(token, chat_id, path, label, state):
    if not path.exists():
        return
    content = path.read_text()
    fp = fingerprint(content)
    key = str(path)
    if state.get(key) == fp:
        return
    if not content.strip():
        state[key] = fp
        return
    msg = f"<b>🔔 {escape_html(label)}</b>\n\n{content}"
    send_message(token, chat_id, msg)
    state[key] = fp
    print(f"[notify] pushed {label} ({len(content)} chars)")


def main():
    global SECRETS_PATH
    ap = argparse.ArgumentParser()
    ap.add_argument("--outdir", default="deliverables")
    ap.add_argument("--state", default="services/notify_state.json")
    ap.add_argument("--secrets", default=str(SECRETS_PATH), help="JSON file with TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID")
    ap.add_argument("--test", action="store_true", help="send a test message and exit")
    args = ap.parse_args()

    if args.secrets != str(SECRETS_PATH):
        SECRETS_PATH = Path(args.secrets)
    token, chat_id = load_secrets()

    if args.test:
        send_message(token, chat_id, "<b>✅ Test message</b> — Bounty agent push channel is live.")
        print("[notify] test message sent")
        return

    state = load_state(Path(args.state))
    outdir = Path(args.outdir)
    pushed = []
    for name, label in (("ALERTS.md", "Fresh bounty alerts"), ("PROPOSALS.md", "Proposal watcher")):
        before = dict(state)
        push_source(token, chat_id, outdir / name, label, state)
        if state != before:
            pushed.append(label)
    Path(args.state).write_text(json.dumps(state, indent=2))
    print(f"[notify] {_dt.datetime.now().isoformat(timespec='seconds')} run complete, pushed={pushed}")


if __name__ == "__main__":
    main()
