# README_LLM.md — Technical reference for coding agents

This document describes the `swapcard-meeting-tool` codebase in detail sufficient for a coding agent to extend or integrate it. Read this before touching any file.

---

## Purpose

A two-stage terminal pipeline for conference meeting preparation:

1. **Stage 1 (`recommend.py`)** — LLM-driven recommendation engine. Retrieval is fully local (local sentence embeddings + BM25, no data leaves the machine); the LLM reasoning uses a user-provided API key (Anthropic, Gemini, or OpenAI). Outputs two local `.md` files.

2. **Stage 2 (`schedule.py`)** — Meeting scheduler. Fetches mutual availability from the Swapcard GraphQL API, then solves a maximum-weight bipartite matching to produce an optimal schedule.

The Node.js retrieval layer (`lib/`, `scripts/`) is called as subprocesses by `recommend.py`; it is unchanged and untouched by the Python layer.

---

## Runtime requirements

- **Node.js ≥ 18** (uses built-in `fetch`, `node:crypto`, `node:util`)
- `npm install` — all deps are pure Node/npm, no native binaries except what `@xenova/transformers` downloads at first use
- First run of `npm run embed` downloads the `bge-small-en-v1.5` model from HuggingFace (~25 MB); after that it is cached by `@xenova/transformers` locally

---

## Directory structure

```
swapcard-meeting-tool/
├── lib/                    # Core Node.js library — import only from here
│   ├── types.ts            # Shared types: Attendee, Candidate
│   ├── sheet.ts            # Google Sheets fetch, parse, cache, search
│   ├── embed.ts            # Local embedding model, cache I/O, cosine similarity
│   └── retrieve.ts         # BM25 index (MiniSearch), BM25-only two-pool retrieval
├── scripts/                # CLI entry points (run via tsx / npm scripts)
│   ├── find.ts             # Name search + full profile lookup
│   ├── embed.ts            # Build/refresh the on-disk embedding cache
│   ├── recall.ts           # PRODUCTION: hybrid (semantic + BM25) retrieval
│   ├── retrieve.ts         # FALLBACK: BM25-only retrieval
│   └── dump.ts             # Batch-export profiles to markdown (test harness only)
├── recommend.py            # Stage 1: LLM orchestration — queries retrieval CLIs,
│                           #   calls LLM API, renders two output .md files
├── schedule.py             # Stage 2: Swapcard availability fetch + bipartite matching
├── output/                 # Generated .md files (git-ignored except .gitkeep)
├── cache/                  # git-ignored: embeddings.bin + meta sidecar, batch dumps
├── requirements.txt        # Python deps (requests numpy scipy anthropic ...)
├── .env.example            # All configuration documented
├── requester_context.example.json  # Input template for recommend.py
├── CLAUDE.md               # Agent workflow spec (primary operational document)
├── README.md               # User-facing overview
├── README_LLM.md           # This file
├── package.json
├── tsconfig.json
└── .gitignore
```

### Which files matter for extensions

| File | Extend? | Notes |
|---|---|---|
| `lib/types.ts` | Yes — add fields to `Attendee` / `Candidate` if the sheet gains columns | All other files import from here |
| `lib/sheet.ts` | Yes — update column mapping if sheet layout changes; add new `Attendee` fields | Hardcodes sheet ID/GID and column indices |
| `lib/embed.ts` | Rarely | Change `EMBED_MODEL` only if switching embedding model; cache format will invalidate |
| `lib/retrieve.ts` | Possibly — add BM25 stopwords; change RRF weights | BM25-only path only |
| `scripts/recall.ts` | Rarely — this is the hot path; touch only to add CLI flags | Read carefully before editing |
| `scripts/find.ts` | Rarely | |

---

## Data structures

### `Attendee` (`lib/types.ts`)

The canonical per-person record. All fields are strings unless noted.

```typescript
{
  id: string;           // stable: "{name-slug}-{10-char-sha1}"; see ID generation below
  firstName: string;
  lastName: string;
  company: string;
  jobTitle: string;
  careerStage: string;
  biography: string;
  expertise: string[];  // split on ";" or newline
  interests: string[];  // split on ";" or newline
  helpOthers: string;   // free-text: what they can offer to others
  needHelp: string;     // free-text: what they're looking for
  country: string;
  seekingWork: string;
  recruitment: string[]; // split on ";" or newline
  swapcardUrl: string;
  linkedinUrl: string;
}
```

### `Candidate` (`lib/types.ts`)

`Attendee` extended with retrieval provenance. Used in `lib/retrieve.ts` output only.

```typescript
Attendee & {
  matchedWanted: string[];   // BM25 terms matched in fitProfile
  matchedOffered: string[];  // BM25 terms matched in needHelp (BM25 path only)
  matchedLateral: string[];  // BM25 terms matched in lateral search
  retrievalScore: number;    // RRF score (higher = better)
}
```

### `EmbedCache` (`lib/embed.ts`)

On-disk format, stored as two sidecar files:

```typescript
{
  model: string;       // e.g. "Xenova/bge-small-en-v1.5"
  dim: number;         // 384 for bge-small
  signature: string;   // "{count}:{12-char-sha1-of-sorted-ids}" — invalidation key
  ids: string[];       // attendee id at index i corresponds to vectors[i]
  vectors: Float32Array[]; // dim-length normalised float32 vectors
}
```

On disk: `{path}.bin` = flat float32 LE buffer; `{path-without-.bin}.meta.json` = JSON sidecar with model/dim/signature/ids.

---

## Sheet parsing and ID generation (`lib/sheet.ts`)

### Data source

```
https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid={SHEET_GID}
```

Defaults:
- `SHEET_ID = "1Uvj0sDZzQJN0gUsccpw0HAf5qjFB6pkbjGMt2QuH6RA"` (EAG London 2026)
- `SHEET_GID = "43679916"`

Override via env vars `SHEET_ID` / `SHEET_GID` at runtime — no `.env` file exists; set them in the shell.

### Column mapping (0-indexed, after the header row)

| Index | Field |
|---|---|
| 0 | firstName |
| 1 | lastName |
| 2 | company |
| 3 | jobTitle |
| 4 | careerStage |
| 5 | biography |
| 6 | expertise |
| 7 | interests |
| 8 | needHelp |
| 9 | helpOthers |
| 10 | country |
| 11 | seekingWork |
| 12 | recruitment |
| 13 | swapcardUrl |
| 14 | linkedinUrl |

The parser finds the header row by scanning for the first row whose column 0 equals `"First Name"`. Rows before that row are skipped. This makes the parser robust to preamble rows.

**If the sheet gains or rearranges columns, update the index constants in `lib/sheet.ts`** and add any new fields to `Attendee` in `lib/types.ts`.

### ID generation

```
id = slugify(firstName + " " + lastName) + "-" + stableHash(source)
```

where `stableHash` = first 10 hex chars of SHA-1, and `source` is:
- The Swapcard person token extracted from `swapcardUrl` if present (`person/([^/?#]+)`)
- Otherwise `"{firstName}|{lastName}|{linkedinUrl}|{company}"`

IDs are stable as long as the Swapcard URL (or the fallback fields) don't change between sheet exports. **Note:** `CLAUDE.md` says "12-char token" — the actual code uses 10 chars. Trust the code.

### In-process cache

`fetchAttendees()` caches the parsed result for 60 minutes **within a single Node process**. Two separate `npm run` invocations each re-download the sheet. This is why batched calls (`find --id a,b,c`) are important for performance — each `npm run` is a new process.

---

## Environment variables

All config lives in `.env` (see `.env.example`). Variables are loaded by both Python scripts via `python-dotenv`.

### Stage 1 (`recommend.py`)
| Variable | Default | Purpose |
|---|---|---|
| `LLM_PROVIDER` | `anthropic` | `anthropic` \| `gemini` \| `openai` |
| `LLM_MODEL` | *(provider default)* | Override the model |
| `ANTHROPIC_API_KEY` | — | Required for Anthropic |
| `GEMINI_API_KEY` | — | Required for Gemini |
| `OPENAI_API_KEY` | — | Required for OpenAI |
| `NUM_RECOMMENDATIONS` | `25` | Target recommendation count |
| `RECALL_TOP` | `150` | Primary pool size |
| `RECALL_TOP_LATERAL` | `40` | Lateral pool size |
| `OUTPUT_DIR` | `output` | Where to write output .md files |

### Stage 2 (`schedule.py`)
| Variable | Default | Purpose |
|---|---|---|
| `SWAPCARD_TOKEN` | — | Bearer JWT; expires ~24h |
| `EVENT_ID` | EAG London 2026 | Base64-encoded Swapcard event ID |
| `DATE_RANGE_START/END` | 2026-05-29…06-01 | Conference window |
| `CANDIDATES_FILE` | `output/meeting_candidates_latest.md` | Schedule input |
| `MY_AVAILABILITY_FILE` | `my_availability.json` | Your Swapcard agenda export |
| `RANKINGS_TO_CONSIDER` | `4,5` | Scores to schedule |
| `PREFER_MEETING_GAP` | `true` | Post-processing gap insertion |

### Node.js scripts only
| Variable | File | Default |
|---|---|---|
| `SHEET_ID` | `lib/sheet.ts` | EAG London 2026 sheet |
| `SHEET_GID` | `lib/sheet.ts` | `43679916` |

Set in shell or prepend: `SHEET_ID=abc npm run find -- "Alice"`.

---

## End-to-end data flow

```
requester_context.json + .env
        │
        ▼
recommend.py
  ├── npm run find (subprocess) ──→ find requester ID
  │
  ├── LLM Call 1 (query construction)
  │   input:  requester profile + extra_context + goals
  │   output: {query, wanted[], lateral[]}
  │
  ├── npm run recall (subprocess) ──→ hybrid shortlist JSON
  │   (semantic RRF BM25, ~190 candidates)
  │
  ├── npm run find --id (subprocess) ──→ full profiles JSON
  │
  ├── LLM Call 2 (pick recommendations)
  │   input:  requester profile + ~190 candidate profiles
  │   output: [{name,rating,why,talking_points,...}, ...]
  │
  └── render output files
      ├── output/recommendations_<name>_<date>.md   (detailed)
      └── output/meeting_candidates_<name>_<date>.md (table)

User reviews + edits candidates table (adjust ratings, remove/add people)

        │
        ▼
schedule.py
  ├── load_candidates() ──→ [(pid, name, weight), ...]
  ├── load_my_busy_slots() ──→ [(start, end), ...]  from my_availability.json
  ├── parse_manual_busy(MANUAL_BUSY) ──→ additional busy intervals
  ├── fetch_all_availability() ──→ {pid: [slot, ...], ...}  (Swapcard GraphQL)
  ├── solve_schedule() ──→ bipartite matching (Hungarian)
  ├── _apply_gap_preference() ──→ post-process gaps (if PREFER_MEETING_GAP=true)
  └── print optimal schedule to stdout
```

**Fallback path** (no embeddings): `scripts/retrieve.ts` → BM25-only two-pool output. Use only when the embedding cache is unavailable.

---

## Retrieval implementation

### BM25 index (`lib/retrieve.ts`)

Built by `buildIndex(attendees)` using MiniSearch. Each attendee is indexed as two fields:

- `fitProfile`: `biography + expertise + interests + helpOthers + jobTitle + company` (joined by newline)
- `needHelp`: the `needHelp` field alone

MiniSearch options: `OR` combine, `prefix: true`, `fuzzy: 0.15`. Terms are lowercased and filtered through `EXTRA_STOPWORDS` (generic EA/AI conference filler: "ai", "research", "effective", "altruism", "impact", etc. — see `lib/retrieve.ts:15–51`).

**If you add domain-specific stopwords, add them to `EXTRA_STOPWORDS` in `lib/retrieve.ts`.**

### Hybrid RRF (`scripts/recall.ts`)

The `fuse()` function in `recall.ts` (distinct from the one in `retrieve.ts`) combines a semantic ranking with a BM25 ranking using **Reciprocal Rank Fusion** with K=60:

```
score(id) = 1/(60 + semRank + 1)   [if id in semantic ranking]
          + 1/(60 + bm25Rank + 1)  [if id in BM25 ranking]
```

Unlike the BM25-only `retrieve.ts`, the hybrid `recall.ts`:
- Does **not** use a separate `offeredKeywords`/`needHelp` BM25 channel (mutual-fit direction is expected in the prose `--query` which embeddings capture)
- Gives equal weight to semantic and BM25 channels (no WEIGHT_WANTED/WEIGHT_OFFERED)
- Uses the lateral keywords to embed a second query vector for the lateral pool (not just BM25)

### Lateral pool (serendipity)

Built by running a second, independent hybrid search over `--lateral` keywords, then **filtering out** everyone already in the primary pool. This guarantees lateral picks are genuinely off-axis — they could not have made the primary list.

### `retrieve.ts` vs `recall.ts`

| | `recall.ts` (hybrid) | `retrieve.ts` (BM25-only) |
|---|---|---|
| Semantic channel | Yes | No |
| Primary fusion | RRF(sem, BM25(wanted)) | RRF(BM25(wanted)×0.7, BM25(offered)×0.3) |
| `offeredKeywords` channel | No | Yes (searches `needHelp` field) |
| Lateral pool | Hybrid (sem + BM25) | BM25-only |
| Requires cache | Yes | No |

Use `recall.ts` by default. Use `retrieve.ts` only when the embedding cache is unavailable.

---

## npm scripts reference

| Script | Entry | Description |
|---|---|---|
| `npm run find -- "<name>"` | `scripts/find.ts` | Fuzzy name search; prints ranked matches |
| `npm run find -- --id <id>` | `scripts/find.ts` | Full readable profile |
| `npm run find -- --id <id1,id2,...>` | `scripts/find.ts` | Batch full profiles — always prefer this |
| `npm run find -- --id <id> --brief` | `scripts/find.ts` | Header line only |
| `npm run find -- --id <ids> --json` | `scripts/find.ts` | Raw JSON output |
| `npm run find -- --refresh "<name>"` | `scripts/find.ts` | Bypass 60-min sheet cache |
| `npm run embed` | `scripts/embed.ts` | Build embedding cache (one-time, few minutes) |
| `npm run embed -- --force` | `scripts/embed.ts` | Force rebuild even if cache seems valid |
| `npm run embed -- --out <path>` | `scripts/embed.ts` | Custom cache path |
| `npm run recall -- <id> --query "..." [--wanted "..."] [--lateral "..."]` | `scripts/recall.ts` | Hybrid retrieval (primary + lateral pools) |
| `npm run recall -- ... --top 100 --top-lateral 30` | `scripts/recall.ts` | Override pool sizes |
| `npm run recall -- ... --ids` | `scripts/recall.ts` | Print only comma-separated primary ids |
| `npm run recall -- ... --json` | `scripts/recall.ts` | JSON output |
| `npm run retrieve -- <id> --wanted "..."` | `scripts/retrieve.ts` | BM25-only fallback |
| `npm run dump` | `scripts/dump.ts` | Batch markdown export (test only) |

All scripts use `tsx` (TypeScript execution without separate compile step).

---

## Embedding cache details

### Files

Default location: `cache/embeddings.bin` (and `cache/embeddings.meta.json`).

- `.bin`: flat little-endian float32 buffer. `N × 384` floats. Index `i` maps to `ids[i]` in the sidecar.
- `.meta.json`: `{ model, dim, signature, ids[] }`. `signature = "{count}:{12-char-sha1-of-sorted-ids}"`.

Both files are git-ignored.

### Invalidation

The cache is stale if:
- `cache.model !== EMBED_MODEL` (model changed)
- `cache.signature !== corpusSignature(attendees)` (sheet changed — different attendees)

`corpusSignature` = SHA-1 over concatenated attendee ids in sheet order, sliced to 12 hex chars.

### Text embedded per attendee (`lib/embed.ts::embedText`)

```
{firstName} {lastName}
{jobTitle} at {company} ({country}) [{careerStage}]
{biography}
expertise: {expertise.join("; ")}
interests: {interests.join("; ")}
offers: {helpOthers}
seeking: {needHelp}
```

If you add fields to `Attendee` that should influence semantic matching, add them here.

---

## Google Docs output (external MCP)

No GDocs code lives in this repo. Output is written by the Claude agent via an external MCP server registered as `gdocs` (`@a-bonus/google-docs-mcp`).

**Setup** (per machine, not committed):
```bash
# One-time auth:
npx -y @a-bonus/google-docs-mcp auth
# Register in Claude Code:
claude mcp add gdocs \
  -e GOOGLE_CLIENT_ID=<your-client-id> \
  -e GOOGLE_CLIENT_SECRET=<your-client-secret> \
  -- npx -y @a-bonus/google-docs-mcp
```

Requires a Google Cloud **Desktop OAuth** client (not service account) with Docs API + Drive API enabled. Refresh token stored at `~/.config/google-docs-mcp/token.json`.

**Output structure:**
- Folder: `EAG Recommendations` (created if absent)
- Doc title: `EAG London 2026 — Recommendations: {Requester Name}`
- One tab per recommendation run: `v1`, `v2`, `v3`, …
- Written via `mcp__gdocs__appendMarkdown` in ~8–9 card chunks

If the gdocs MCP tools are not available in the session, the server is not configured on the current machine.

---

## Hardcoded assumptions to know about

| What | Where | Value / implication |
|---|---|---|
| Event name | README, CLAUDE.md | "EA Global London 2026" |
| Default sheet ID | `lib/sheet.ts:5–7` | Hardcoded; override with `SHEET_ID` env var |
| Header detection | `lib/sheet.ts:14` | Finds row where col 0 = `"First Name"` |
| Column order | `lib/sheet.ts:79–95` | Fixed 0–14 mapping; update if sheet rearranged |
| Embedding model | `lib/embed.ts: EMBED_MODEL` | `"Xenova/bge-small-en-v1.5"` (384-dim) |
| Cache default path | `scripts/embed.ts`, `scripts/recall.ts` | `cache/embeddings.bin` |
| Primary pool size | `scripts/recall.ts`, `RECALL_TOP` in `.env` | 150 |
| Lateral pool size | Same | 40 |
| RRF_K constant | Both retrieval paths | 60 |
| BM25 stopwords | `lib/retrieve.ts:15–51` | EA/AI-domain filler terms |
| ID hash length | `lib/sheet.ts:31` | 10 chars (old docs said 12 — code is authoritative) |
| Swapcard ID format | `schedule.py: load_candidates` | Both `CommunityProfile_*` and `EventPeople_*` IDs accepted (API-verified) |
| Year in MANUAL_BUSY | `schedule.py: parse_manual_busy` | Hardcoded to 2026 |
| Default LLM models | `recommend.py: PROVIDER_DEFAULTS` | `claude-sonnet-4-6` / `gemini-2.5-flash` / `gpt-5.5` |

---

## `recommend.py` internals

### Three subprocess calls (each re-downloads the sheet — ~2–4 s overhead each)
1. `npm run find -- "<name>" --json` → find requester ID (or use `swapcard_id` from context)
2. `npm run recall -- <id> --query "..." --wanted "..." --lateral "..." --json` → candidate list
3. `npm run find -- --id "id1,...id190" --json` → full attendee profiles

### LLM calls
- **Call 1** (cheap — `max_tokens=1024`): generate `{query, wanted[], lateral[]}` from requester profile + goals
- **Call 2** (main — `max_tokens=8192`): pick N recommendations, return `{"recommendations": [...]}`

Both calls use the same model (configured in `.env`). JSON is returned directly; both Anthropic and Gemini have JSON response modes. OpenAI uses `response_format={"type": "json_object"}`.

### Output rendering
The JSON array from Call 2 is rendered twice by Python:
- `render_detailed_md()` → grouped by rating (5 first), one card per person
- `render_candidates_table()` → 5-column pipe table; Swapcard ID extracted from URL last path segment

The `output/meeting_candidates_latest.md` symlink is overwritten on each run (points to the most recent dated file).

## `schedule.py` internals

### Key changes vs original `timeslot_optimisation/schedule.py`
- All config moved to `.env` / `python-dotenv` (TOKEN, EVENT_ID, DATE_RANGE, file paths, etc.)
- `load_my_busy_slots`: removed `break` — now processes ALL `data.agenda` responses and deduplicates by `(start, end)`
- `load_candidates`: removed `RXZlbn` prefix filter — both `CommunityProfile_*` and `EventPeople_*` IDs work with `MeetSlotsQuery` (confirmed by live API test)
- `_fetch_slots`: exits with a clear message on HTTP 401/403 (expired token)
- `_apply_gap_preference`: new post-processing pass after `linear_sum_assignment`
- Scheduled tuples now include `pid` as 6th element: `(start, end, name, weight, slot_dict, pid)`

### Gap post-processing (`_apply_gap_preference`)
Runs after the Hungarian algorithm. Never drops meetings. Algorithm:
1. Sort schedule by start time
2. Find consecutive pairs with < 25 min gap; sort by `max(rating_i, rating_{i+1})` descending
3. For each pair: try to move the LATER meeting to an alternative available slot that is ≥ 25 min after the current meeting ends, not taken, not in my_busy, and fits before the next meeting
4. If found, apply; otherwise leave back-to-back as-is

## Extension guide

### Adding new sheet columns

1. Add the field to `Attendee` in `lib/types.ts`
2. Add the column index mapping in `lib/sheet.ts` inside the `Attendee` construction block (lines ~79–96)
3. Decide whether the field should influence:
   - **BM25**: add it to the `toDoc`/`fitProfile` assembly in `lib/retrieve.ts` (line ~67–78)
   - **Semantic embedding**: add it to `embedText` in `lib/embed.ts`; then re-run `npm run embed -- --force`

### Adding a new CLI script

1. Create `scripts/my-script.ts`
2. Add a `"my-script": "tsx scripts/my-script.ts"` entry to `package.json` scripts
3. Run as `npm run my-script -- <args>`
4. Import only from `lib/` (never from other scripts)

### Changing the embedding model

1. Update `EMBED_MODEL` in `lib/embed.ts`
2. Update `EMBED_DIM` if different from 384
3. Run `npm run embed -- --force` to rebuild cache

### Adapting to a different conference / sheet

1. Set `SHEET_ID` and `SHEET_GID` env vars
2. Verify column order matches; update `lib/sheet.ts` if not
3. Update event name references in `CLAUDE.md` and `README.md`
4. Rebuild the embedding cache: `npm run embed -- --force`

### Adding programmatic / API-key-driven invocation

Currently, the LLM layer is the interactive Claude session. To add API-key-driven operation:

1. Add `ANTHROPIC_API_KEY` (or equivalent) to the env var documentation
2. Write a new script (e.g., `scripts/recommend.ts`) that:
   - Calls `fetchAttendees`, `recall` logic, `findAttendee` (all importable from `lib/`)
   - Passes retrieved text to the Anthropic SDK (install `@anthropic-ai/sdk`)
   - Outputs the result to stdout or GDocs
3. The retrieval layer does not need to change — it is already a pure function that returns ranked `Candidate[]` arrays

---

## Module dependency graph

```
lib/types.ts
     ↑
lib/sheet.ts
     ↑ ─────────────────────────────────────────────────────────┐
lib/embed.ts                                             lib/retrieve.ts
     ↑                                                          ↑
scripts/embed.ts    scripts/recall.ts (imports both lib/embed + lib/retrieve)
                    scripts/retrieve.ts
                    scripts/find.ts
                    scripts/dump.ts
```

`lib/` modules never import from `scripts/`. Scripts import only from `lib/`.

---

## `.claude/settings.json`

Pre-approves a set of npm script commands for Claude Code's permission system. Currently references the original author's machine path — you may need to update it for your own machine if Claude Code prompts for approval. Not security-critical; just affects interactive approval dialogs.
