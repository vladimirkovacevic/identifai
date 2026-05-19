# identifAI — Plan

A local daemon on this Linux machine that lets the user send a photo from
iPhone Telegram to a personal bot and receive a Serbian-language summary
identifying what is in the photo (plant, object, animal, landmark…).

---

## 1. Open questions (decide before coding)

1. **Identification strategy.** Two viable options:
   - **A — Vision only:** Claude vision identifies + writes the Serbian
     summary in a single API call. Simplest. Best for common subjects.
   - **B — Vision + web search (recommended, matches the brief):**
     Claude vision identifies the subject, Claude's built-in
     `web_search` tool fetches authoritative pages, Claude summarizes in
     Serbian and cites sources. Better for obscure species, regional
     names, toxicity/edibility warnings, current info. When web_search 
     is on give to the user as soon as posible response summary from claude 
     and then, in the next message add additional results/conclusions/fun facts
     and  references from web search. Perform this web search also with the
     image and collect top, most reliable results and combine with what claude 
     previously gave to also check consistency.

   Use **B** by default, but make possible to easily switch to A.

2. **Telegram access mode.** Long polling (bot pulls updates from
   Telegram). No public endpoint needed, no port forwarding, works
   behind NAT. Webhooks rejected — would require a public HTTPS URL.

3. **Secrets.** Telegram bot token + Anthropic API key live in
   `~/.config/identifAI/.env` (chmod 600). Not in the repo.

4. **Access control.** Bot must refuse anyone but the owner. Whitelist
   a single Telegram `user_id` (or chat_id) in config. Without this,
   anyone who finds the bot username can use your API credits.

5. **Cost / rate limiting.** Per-image cost: ~1 vision call + a few
   search calls + 1 summarization call. Set a daily image cap
   (e.g. 50/day) in config; on breach, reply with a polite Serbian
   "limit reached" message. Avoids surprise bills.

---

## 2. Architecture (smallest thing that works)

```
iPhone Telegram ──► Telegram servers ──► [long poll]
                                              │
                                              ▼
                                   identifAI daemon (Python)
                                              │
                            ┌─────────────────┼─────────────────┐
                            ▼                 ▼                 ▼
                       Telegram API     Claude Code CLI    Local logs
                       (download         (claude -p:        (rotating
                        photo,           vision +           file, no
                        send reply)      web_search +       images
                                         Serbian summary)   stored)
```

One process. No database. No queue. No webhook server. Photo is
processed in-memory and discarded after the reply is sent.
Reply text (not the image) is saved under
`responses/<UTC-timestamp>/reply.txt` for retrospective review.

---

## 3. Components

### 3.1 `identifai/bot.py` — Telegram loop
- Library: `python-telegram-bot` (v21+, async).
- Handles `/start`, `/help`, and `photo` messages only.
- On photo: download the highest-resolution variant to a `bytes` buffer,
  pass to the identifier, send the reply text back to the same chat.
- Sends a "🔎 tražim..." (searching…) placeholder immediately so the user
  sees progress while the API call runs.
- Rejects non-whitelisted users with a fixed Serbian message.

### 3.2 `identifai/identify.py` — headless Claude Code call

**Why CLI instead of Anthropic SDK:** the user has Claude Code seat
credits via the organization but no Anthropic API budget. The `claude`
CLI in headless mode (`claude -p`) authenticates against the existing
Claude Code subscription, so calls draw from credits, not the API
billing bucket. Bonus: vision (`Read` on the image file) and web search
(`WebSearch` tool) are built in — no separate SDK, no SerpAPI key.

- Single function `identify(image_path: Path) -> str`.
- Writes the photo to a temp file, then invokes:
  ```
  claude -p <PROMPT> \
         --allowedTools "Read,WebSearch" \
         --output-format text \
         --model <MODEL>
  ```
  The prompt instructs the model to `Read` the temp file path.
- Strategy switch (see §1.A vs §1.B):
  - **B (default):** `--allowedTools "Read,WebSearch"`.
  - **A (vision only):** `--allowedTools "Read"` — drops search.
  Controlled by a single config flag `USE_WEB_SEARCH=true|false`.
- System prompt (Serbian, concise): instructs the model to
  1. `Read` the image at the given path.
  2. Identify the primary subject (latin + Serbian common name when
     applicable).
  3. If web search is allowed, use it to confirm and pull 2–4
     authoritative facts (habitat, edibility/toxicity, care notes, or
     whatever fits the subject type).
  4. Reply in **Serbian (latinica)**, max ~150 words, with a short
     "Izvori:" line listing source URLs (omit if vision-only).
  5. If uncertain, say so — do not invent.
- Temp file lives under `$XDG_RUNTIME_DIR/identifAI/` and is unlinked
  in a `finally` block after the call returns.
- Timeout: 90 s. On timeout/non-zero exit, reply with a Serbian
  "došlo je do greške" message and log stderr.

### 3.3 `identifai/config.py` — Config loader
- Reads `~/.config/identifAI/.env`:
  - `TELEGRAM_BOT_TOKEN` (required)
  - `ALLOWED_TELEGRAM_USER_IDS` (required, comma-separated)
  - `DAILY_IMAGE_LIMIT` (default 50)
  - `MODEL` (default `claude-sonnet-4-6` — cheaper per credit;
    upgrade to `claude-opus-4-7` if accuracy is short)
  - `USE_WEB_SEARCH` (default `true`)
  - `CLAUDE_BIN` (default `claude` — absolute path if `claude` is not
    on the daemon's `PATH` under systemd)
  - `RESPONSES_DIR` (default `./responses` relative to project root)
- No `ANTHROPIC_API_KEY` — the CLI uses the user's existing Claude
  Code login (`~/.claude/` credentials).
- On startup, runs `claude --version` once; if it fails, exits with a
  loud error pointing the user to log in via `claude` first
  (Rule 12).

### 3.4 `identifai/__main__.py` — Entry point
- Loads config, starts the bot loop, handles SIGTERM cleanly.
- `python -m identifai` runs the daemon.

### 3.5 Logging
- `logging` to `~/.local/state/identifAI/identifai.log` with rotation
  (10 MB × 3). Logs: user_id, timestamp, photo size, CLI latency,
  CLI exit code, errors. **No image content, no reply text in logs** —
  log stays small; reply text instead goes to
  `responses/<ts>/reply.txt` (see §2). Image bytes are never persisted.
- Token / credit usage is not exposed by `claude -p`'s plain-text
  output. If we need it, switch to `--output-format json` and parse
  the `usage` field. Deferred.

---

## 4. Daemon setup (systemd --user)

`~/.config/systemd/user/identifai.service`:

```ini
[Unit]
Description=identifAI Telegram identification daemon
After=network-online.target

[Service]
Type=simple
ExecStart=/home/digital/identifAI/.venv/bin/python -m identifai
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

Enable with `systemctl --user enable --now identifai`. `loginctl
enable-linger digital` so it survives logout.

---

## 5. Project layout

```
identifAI/
├── CLAUDE.md
├── PLAN.md
├── pyproject.toml          # deps: python-telegram-bot, python-dotenv
├── README.md               # setup + bot creation steps
├── responses/              # per-photo reply text, timestamped
├── identifai/
│   ├── __init__.py
│   ├── __main__.py
│   ├── bot.py
│   ├── identify.py
│   └── config.py
└── tests/
    └── test_identify.py    # see §7
```

Single package, four files. No premature abstraction (Rule 2).

---

## 6. Setup steps the user has to do once
When run for the first time walk user through obtaining all necessary Telegram configurations
1. Talk to **@BotFather** on Telegram → `/newbot` → get bot token.
2. Message your own bot once, then visit
   `https://api.telegram.org/bot<TOKEN>/getUpdates` to find your
   `user_id`. Put it in `.env` as `ALLOWED_TELEGRAM_USER_IDS`.
3. Make sure `claude` CLI is installed and logged in to the
   organization account (`claude` → follow auth flow once). No
   Anthropic API key needed — the daemon uses the existing Claude
   Code subscription.
4. `python -m venv .venv && .venv/bin/pip install -e .`
5. `systemctl --user enable --now identifai`.
6. Send a photo from iPhone Telegram to the bot.

---

## 7. Verification (success criteria — Rule 4)

The daemon is "done" when **all** of these hold:

- [ ] Sending a clear plant photo from iPhone returns a Serbian reply
      within ~15 s containing: subject name (latin + Serbian when
      applicable), 2–4 facts, source URLs.
- [ ] Sending a non-image message returns a short Serbian usage hint.
- [ ] A photo from a non-whitelisted Telegram account is rejected with
      a fixed Serbian message and logged.
- [ ] Killing the process (`kill -TERM`) and re-sending a photo works
      after systemd auto-restart.
- [ ] Daily limit exceeded → polite Serbian "limit reached" reply,
      no API call made.
- [ ] Log file contains one structured line per request, with **no**
      image bytes and **no** reply text.
- [ ] Ambiguous photo (e.g. blurry leaf) elicits an "nisam siguran"
      ("not sure") reply, not a confident guess. (Rule 12 — fail loud.)

Tests in `tests/test_identify.py` use 3–4 reference photos checked into
`tests/fixtures/` and assert that the reply mentions the expected
species name. This encodes WHY the daemon exists (Rule 9): correct
identification, not just "an API call happened".

---

## 8. Out of scope (do not build unless asked)

- Multi-user / multi-tenant support.
- Storing photos or chat history.
- A web UI, an admin panel, metrics dashboards.
- Voice notes, video, or document handling.
- Languages other than Serbian.
- A queue / worker pool — single in-process handler is enough for one
  user. Telegram's library already serializes per-chat updates.

---

## 9. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Telegram token leak via repo | `.env` outside repo, `.gitignore` covers it anyway |
| Stranger finds bot, burns org Claude Code credits | User-ID whitelist + daily cap |
| `claude` CLI session expires under systemd | On non-zero exit code referencing auth, daemon stops and logs a loud "re-login required" message instead of looping |
| Org policy forbids automated/headless Claude Code use | Confirm with admin before deploying; fallback path is a local Ollama vision model (out of scope here, but noted) |
| Model hallucinates wrong species | Web search + "nisam siguran" prompt clause + source URLs the user can verify |
| Telegram long-poll connection drops | `python-telegram-bot` reconnects; systemd restarts the process on hard failure |
| Large photos slow upload | Telegram's `get_file` already gives a compressed variant; pick `photo[-1]` (highest) but it's still <5 MB |
| CLI startup latency stacks under load | Single user, low volume → not a concern. If it becomes one, switch to `claude` in interactive/JSON-stream mode (out of scope) |

---

## 10. Implementation order

1. Sanity check: from the shell, run
   `claude -p "describe this image" --allowedTools Read <path-to-test.jpg>`
   manually and confirm a sensible reply comes back using org
   credentials. If this fails, nothing else will work.
2. Skeleton: `config.py`, `__main__.py`, empty bot that echoes "ok".
3. Add identifier with **vision only** (`USE_WEB_SEARCH=false`) —
   verify Serbian output works end-to-end via the CLI.
4. Flip `USE_WEB_SEARCH=true` and add source citations.
5. Add whitelist + daily cap.
6. systemd unit + lingering.
7. Reference-photo tests.

Stop after each step, send a real photo, confirm before moving on
(Rule 10 — checkpoint).

