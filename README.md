# EAG Recommendation Tool

Generates personalised meeting recommendations from the EA Global London 2026 attendee sheet, then produces an optimised schedule based on mutual availability.

Everything runs locally in the terminal. Retrieval uses a local hybrid search (embeddings + BM25) — no data leaves the machine. The LLM reasoning uses your own API key.

## Quick start

```bash
npm install
pip install -r requirements.txt
cp .env.example .env          # fill in at minimum LLM_PROVIDER + the matching API key
npm run embed                  # one-time: build the local embedding cache (~3 min)
```

Create your context file:
```bash
cp requester_context.example.json requester_context.json
# edit requester_context.json — name, goals, extra_context
```

## Stage 1 — Recommendations

```bash
python recommend.py            # uses requester_context.json by default
python recommend.py my_ctx.json  # or pass a custom file
```

Two output files appear in `output/`:
- `recommendations_<name>_<date>.md` — detailed cards with why/talking-points/opener, sorted rating 5 → 1
- `meeting_candidates_<name>_<date>.md` — 5-column table for the scheduler (also symlinked to `output/meeting_candidates_latest.md`)

Review the detailed file, then edit the candidates table: adjust ratings, delete people
you don't want to meet, add anyone you found manually (paste their Swapcard profile URL
into the `Link` column; the `Swapcard ID` column is auto-extracted from the URL).

## Stage 2 — Schedule optimisation

Export your Swapcard availability first (see below), then:

```bash
python schedule.py
```

Prints the optimal schedule to stdout. Does **not** book meetings — you send requests
manually so you can write a personal message to each person.

## Configuration

All settings live in `.env` (copy `.env.example`). Key variables:

| Variable | Default | Notes |
|---|---|---|
| `LLM_PROVIDER` | `anthropic` | `anthropic` \| `gemini` \| `openai` |
| `LLM_MODEL` | *(provider default)* | `claude-sonnet-4-6` / `gemini-2.5-flash` / `gpt-5.5` |
| `ANTHROPIC_API_KEY` | — | Required if using Anthropic |
| `GEMINI_API_KEY` | — | Required if using Gemini |
| `OPENAI_API_KEY` | — | Required if using OpenAI |
| `NUM_RECOMMENDATIONS` | `25` | How many recommendations to aim for |
| `SWAPCARD_TOKEN` | — | Bearer JWT from browser DevTools; expires ~24h |
| `EVENT_ID` | EAG London 2026 | Base64-encoded Swapcard event ID |
| `CANDIDATES_FILE` | `output/meeting_candidates_latest.md` | Input for schedule.py |
| `MY_AVAILABILITY_FILE` | `my_availability.json` | Your Swapcard agenda export (talks, meetups) |
| `MANUAL_BUSY_FILE` | `manual_busy.json` | Blocked times outside Swapcard (travel, meals) |
| `RANKINGS_TO_CONSIDER` | `4,5` | Which relevance scores to schedule |
| `PREFER_MEETING_GAP` | `true` | Insert 25-min buffer between meetings when possible |

## Exporting your Swapcard availability

The scheduler needs your existing agenda to avoid double-booking:

1. Log into Swapcard and navigate to your schedule page.
2. Open DevTools (F12) → Network tab.
3. Refresh the page.
4. Find requests to `api/graphql` — look for responses containing `"agenda"` data.
5. Right-click → Copy response → paste into a file called `my_availability.json`
   as a JSON array: `[<response1>, <response2>, ...]`.

Getting a fresh `SWAPCARD_TOKEN`:

1. In the same Network tab, click any request.
2. Headers → find `Authorization: Bearer <token>`.
3. Copy the token value (everything after `Bearer `) into `.env`.

## Adapting to a different event

```bash
SHEET_ID=<new-id> SHEET_GID=<new-gid> npm run embed -- --force
# update EVENT_ID, DATE_RANGE_START, DATE_RANGE_END in .env
```

## Changing sheet or event

- `SHEET_ID` / `SHEET_GID` env vars point to a different Google Sheet
- If the sheet column order changes, update `lib/sheet.ts`

## Notes

- **No auto-booking.** The scheduler outputs a plan; you send Swapcard meeting requests
  manually. This is intentional — a personal message explaining *why* you want to talk
  performs far better than an auto-generated one.
- **Context window.** ~190 full profiles × ~400 tokens ≈ 90k tokens total for the
  recommendation call. Fits in 200k (Sonnet) with room to spare.
- **Bearer token TTL.** The Swapcard token expires in ~24h. Refresh it in `.env` before
  each Stage 2 run.
- **Embedding cache.** Stored in `cache/` (git-ignored). Rebuilt automatically
  when the sheet changes. First build takes ~3 minutes.
