# identifAI

Local daemon: send a photo from your iPhone Telegram to a private bot
and get back a Serbian-language identification (plant, animal, object,
landmark). Vision + web search are handled by the local `claude` CLI in
headless mode, so calls draw from your Claude Code subscription — no
Anthropic API key required.

See [PLAN.md](PLAN.md) for the design.

---

## One-time setup

### 1. Create the Telegram bot
1. Open Telegram, message **@BotFather**.
2. `/newbot` → choose a name and a username ending in `bot`.
3. Copy the token BotFather gives you.

### 2. Find your numeric user_id

You need the **numeric** id (a stable integer), not your @username.
Pick whichever is easier:

**Easy:** Message **@userinfobot** on Telegram → it replies with your id.

**Manual:** Send any message to your new bot from your iPhone, then open
in a browser (replace `<TOKEN>`):
`https://api.telegram.org/bot<TOKEN>/getUpdates` — find
`"from": {"id": 123456789, ...}`.

### 3. Fill in the .env
```bash
mkdir -p ~/.config/identifAI
cp .env.example ~/.config/identifAI/.env
chmod 600 ~/.config/identifAI/.env
$EDITOR ~/.config/identifAI/.env       # paste the token and user_id
```

The two required keys are spelled exactly:

- `TELEGRAM_BOT_TOKEN` — from BotFather
- `ALLOWED_TELEGRAM_USER_IDS` — your numeric id (comma-separated if you
  want to allow more than one person; e.g. `123456,789012`)

Anything else (`TELEGRAM_CHAT_ID`, etc.) is ignored — the daemon
whitelists on Telegram **user_id**, not chat_id.

### 4. Install
```bash
python3 -m venv .venv
.venv/bin/pip install -e .
```

### 5. Verify `claude` CLI is logged in
```bash
claude --version           # should print a version
claude -p "ping"           # should reply, using your org subscription
```

### 6. Run the daemon
```bash
.venv/bin/python -m identifai
```
Logs stream to stderr and to `~/.local/state/identifAI/identifai.log`.
Send a photo to your bot from iPhone Telegram — you'll see a
"🔎 tražim…" placeholder, then the Serbian reply within ~15-30 s
(first call may be slower while `web_search` runs).

Saved replies land in `responses/<UTC-timestamp>/reply.txt`. The image
itself is never persisted.

---

## Configuration reference (`~/.config/identifAI/.env`)

| Key | Default | Notes |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — (required) | From @BotFather |
| `ALLOWED_TELEGRAM_USER_IDS` | — (required) | Comma-separated numeric ids |
| `DAILY_IMAGE_LIMIT` | `50` | Per-process counter, resets at UTC midnight |
| `MODEL` | `claude-sonnet-4-6` | Set to `claude-opus-4-7` if accuracy is short |
| `USE_WEB_SEARCH` | `true` | Set `false` for vision-only (faster, no citations) |
| `CLAUDE_BIN` | `claude` | Absolute path if `claude` not on PATH (e.g. under systemd) |
| `RESPONSES_DIR` | `./responses` | Where reply texts are saved |

---

## Run under systemd (`--user`)

```bash
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/identifai.service <<'EOF'
[Unit]
Description=identifAI Telegram identification daemon
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=%h/identifAI
ExecStart=%h/identifAI/.venv/bin/python -m identifai
Restart=on-failure
RestartSec=5
# claude CLI lives in ~/.local/bin which is not on systemd's default PATH.
Environment=PATH=%h/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now identifai
loginctl enable-linger "$USER"        # survives logout / reboot
```

### Day-to-day commands

```bash
systemctl --user status identifai          # health + last few log lines
systemctl --user restart identifai         # after code or config edits
systemctl --user stop identifai            # pause the bot
systemctl --user start identifai           # resume
systemctl --user disable --now identifai   # turn it off and forget it
journalctl --user -u identifai -f          # live tail of the daemon log
journalctl --user -u identifai -n 50       # last 50 log lines (one-shot)
```

Per-query timing appears in the log as a single line, e.g.:

```
result=ok user_id=… bytes=… model=… search=True
  ack_ms=…   ← time to send the "🔎 tražim…" placeholder
  download_ms=…  ← Telegram photo download
  identify_ms=…  ← claude -p (the dominant cost)
  send_ms=…  ← Telegram reply send
  save_ms=…  ← reply.txt write
  total_ms=…
```

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `missing required env var ...` | `.env` not at `~/.config/identifAI/.env` | Re-do step 3 above |
| `'claude' not found on PATH` | systemd's PATH doesn't include `~/.local/bin` | Set `CLAUDE_BIN=/home/<user>/.local/bin/claude` in `.env` |
| Bot replies "Žao mi je, ovaj bot je privatan." | Your `user_id` is not in `ALLOWED_TELEGRAM_USER_IDS` | Double-check the id (no spaces, comma-separated) |
| Bot replies "Došlo je do greške" | `claude -p` failed | `journalctl --user -u identifai -n 50` to see stderr |
| `claude` session expired | OAuth token stale | Run `claude` interactively once to re-auth |
