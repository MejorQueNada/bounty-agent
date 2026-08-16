# Bounty Agent

A self-hosted agent setup that **triages sats-paying open-source bounties** and
pushes the interesting ones to you over Telegram. Built with:

- **OpenClaw** — the agent gateway that runs the workflow and talks to Telegram
- **Alby Hub** — your self-hosted Lightning wallet (NWC for the agent to use)
- **A Telegram bot** — so the agent can ping you and you can steer it by text
- **Four stdlib-only scripts** — the deterministic part that does the real work

This is the *whole system*, not just one piece: `scout` finds bounty
candidates, `watch` tracks your in-flight proposals for maintainer replies,
`notify` pushes changes to Telegram, and `verify` cross-checks your records
against live reality so you can trust what the agent tells you.

## What it does

| Script | Job | Output |
|---|---|---|
| `services/scout.py` | Polls Lightning Bounties, Stacker News, and getAlby issues; scores + risk-flags them | `deliverables/ALERTS.md` |
| `services/watch_proposals.py` | Polls the issues in `proposals.json` for replies from maintainers | `deliverables/PROPOSALS.md` |
| `services/notify_telegram.py` | Diffs the deliverables and sends changes as Telegram messages | Telegram |
| `services/verify.py` | Reconciles `proposals.json` / `PROPOSALS.md` / live GitHub / ledger | exit code + summary |

All four are stdlib-only Python, run on cron, and log to a single file. The
tests (`scripts/run_tests.sh`, 55 cases) are hermetic — no network, no
secrets.

## Quick start

Follow **[SETUP.md](SETUP.md)** — it walks through Alby Hub, the Telegram
bot, the OpenClaw channel config, and wiring the scripts to cron (~30 min).

```
git clone https://github.com/MejorQueNada/bounty-agent
cd bounty-agent
cp examples/proposals.example.json services/proposals.json
bash scripts/run_tests.sh          # 55 tests, no secrets needed
```

## License

MIT — see [LICENSE](LICENSE).