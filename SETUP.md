# SETUP — OpenClaw + Alby Hub + Telegram bounty triage

This builds the exact system shown in the README: an OpenClaw agent with an
Alby wallet and a Telegram interface that triages open-source bounties.

Roughly 30 minutes. No hosted services required — everything runs on your own
box or a small VPS.

---

## 0. Prerequisites

- A machine you control (Linux/macOS) with Python 3.10+
- [OpenClaw](https://opencode.ai) installed and working (`openclaw --version`)
- A GitHub account and a **fine-grained or classic PAT** with `public_repo`
  scope
- [Alby Hub](https://getalby.com/hub) running somewhere you can reach (self-host
  or the hosted hub)

## 1. Alby Hub: create an NWC app for the agent

1. Open your Alby Hub web UI.
2. **Apps → Create App** → name it `bounty-agent`.
3. Note the **NWC connection string** (`nostr+walletconnect://...`). This is a
   secret — it authorizes spending. Keep it in your secrets file only.
4. In the app's scopes, allow **get balance, make invoice, pay invoice**.
   Restrict by budget if you want.

> If you use the hosted Alby Hub, the connection string is in **Settings →
> Apps**. The flow is identical.

## 2. Telegram bot via @BotFather

1. In Telegram, message **@BotFather** → `/newbot`.
2. Name it whatever you like (e.g. `bounty-agent`) — the handle becomes
   `@your_handle`.
3. Copy the **bot token** it gives you. Secret.
4. Message your new bot once (press Start) so you have a chat with it. Your
   **chat id** is needed for the notifier. Find it by messaging
   `@userinfobot` — it tells you your numeric id.

## 3. Secrets file

Create `~/.config/bounty-agent/secrets.json` (or anywhere — point the scripts
at it with `--secrets`):

```json
{
  "GITHUB_TOKEN": "github_pat_...",
  "TELEGRAM_BOT_TOKEN": "123456:ABC-DEF...",
  "TELEGRAM_CHAT_ID": 123456789
}
```

```bash
chmod 600 ~/.config/bounty-agent/secrets.json
```

This file is gitignored and never read by anyone but you and the local scripts.

## 4. OpenClaw: Telegram channel

Add a Telegram channel to your OpenClaw config (`~/.openclaw/openclaw.json`).
The exact keys depend on your OpenClaw version — see `openclaw doctor` and the
[OpenClaw channel docs](https://opencode.ai). In essence you are telling
OpenClaw to:

- attach to the bot you created in step 2 (botToken = your bot token)
- allow only **your** chat id in DMs (`dmPolicy: allowlist`,
  `allowFrom: [<your chat id>]`, `defaultTo: <your chat id>`)
- make only your chat id able to issue privileged agent commands
  (`commands.ownerAllowFrom: ["telegram:<your chat id>"]`)

Restart the gateway: `systemctl --user restart openclaw-gateway` (or however
you run it) and confirm the log shows your bot name starting the provider.
Then **text the bot** — OpenClaw should respond. That's the two-way interface.

> OpenClaw exposes the agent to the LAN/web through the gateway; keep it bound
> to loopback and never add public ports unless you know what you're doing.

## 5. The scripts

Clone this repo wherever you keep tools:

```bash
git clone https://github.com/MejorQueNada/bounty-agent
cd bounty-agent
cp examples/proposals.example.json services/proposals.json
bash scripts/run_tests.sh   # 55 tests, no secrets, no network
```

### scout.py — find bounties

```bash
python3 services/scout.py --outdir deliverables --secrets ~/.config/bounty-agent/secrets.json
```

Writes `deliverables/ALERTS.md` (and JSON reports). Flags archived repos,
contested issues, and low-trust rewarders. Tune `--min-reward` (sats) and
`--langs` to your skills.

### watch_proposals.py — track in-flight proposals

Add the issues you've contacted to `services/proposals.json`:

```json
{
  "proposals": [
    {"repo": "owner/repo", "issue": 123, "title": "The task", "note": "Contacted 2026-08-16"}
  ],
  "rejected_candidates": []
}
```

```bash
python3 services/watch_proposals.py \
  --config services/proposals.json \
  --outdir deliverables \
  --owner your-github-username \
  --secrets ~/.config/bounty-agent/secrets.json
```

Writes `deliverables/PROPOSALS.md`, marking **NEW REPLY** when a maintainer
responds after you. First run seeds from your own latest comment, so old
history is never re-flagged.

### notify_telegram.py — push to Telegram

```bash
python3 services/notify_telegram.py \
  --outdir deliverables \
  --state services/notify_state.json \
  --secrets ~/.config/bounty-agent/secrets.json
python3 services/notify_telegram.py --test --secrets ~/.config/bounty-agent/secrets.json
```

First run with `--test` sends a test message. The notifier fingerprints
`ALERTS.md` / `PROPOSALS.md` and only pushes when content actually changed
(dedupe state in `notify_state.json`).

### verify.py — trust the records

```bash
python3 services/verify.py \
  --root . \
  --config services/proposals.json \
  --proposals-md deliverables/PROPOSALS.md \
  --owner your-github-username \
  --secrets ~/.config/bounty-agent/secrets.json
```

Reconciles `proposals.json` ↔ `PROPOSALS.md` ↔ live GitHub ↔ ledger, exits
non-zero on any discrepancy. Run it before trusting an agent's claim that
"the maintainer replied" — it catches fabrication.

## 6. Cron

Add to your crontab (adjust paths). All output appends to `bounty-agent.log`:

```cron
# every 3h on the hour: scout new bounties + notify
0 */3 * * *  cd /path/to/bounty-agent && python3 services/scout.py --outdir deliverables --secrets ~/.config/bounty-agent/secrets.json >> bounty-agent.log 2>&1; python3 services/notify_telegram.py --outdir deliverables --state services/notify_state.json --secrets ~/.config/bounty-agent/secrets.json >> bounty-agent.log 2>&1
# every 3h at :15: watch proposals + notify
15 */3 * * * cd /path/to/bounty-agent && python3 services/watch_proposals.py --config services/proposals.json --outdir deliverables --owner your-github-username --secrets ~/.config/bounty-agent/secrets.json >> bounty-agent.log 2>&1; python3 services/notify_telegram.py --outdir deliverables --state services/notify_state.json --secrets ~/.config/bounty-agent/secrets.json >> bounty-agent.log 2>&1
```

Confirm with `grep bounty-agent.log | tail` after the first run.

## 7. First-run sanity checklist

- [ ] `bash scripts/run_tests.sh` → all 55 pass
- [ ] `notify_telegram.py --test` → message arrives in Telegram
- [ ] scout run writes `deliverables/ALERTS.md`
- [ ] you can text the bot and OpenClaw answers
- [ ] `verify.py` exits 0 on a clean state

## Security notes

- The NWC string and bot token **are** money/control. `chmod 600` your secrets
  file, gitignore it, never paste it into chat or commit it.
- Keep the OpenClaw gateway on `127.0.0.1`. Don't add public ports or tunnels
  unless you understand the exposure.
- The scripts hit GitHub's public API with your PAT; per-key cache TTLs keep you
  inside rate limits. No secret ever leaves your machine.
- Free/data-collecting LLM models are fine for these scripts' own logic, but if
  you feed them client code or data, use a paid zero-retention model.