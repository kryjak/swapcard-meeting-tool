# EAG Recommendation Tool

## Two-stage pipeline

The tool runs in two stages from the terminal:

**Stage 1 — Recommendations** (`python recommend.py`):
- Reads `requester_context.json` (name, extra context, goals)
- Makes two LLM API calls: one to build a search query, one to pick recommendations
- Drives the Node.js retrieval scripts via subprocess
- Outputs `output/recommendations_<name>_<date>.md` (detailed cards, rating 5→1)
  and `output/meeting_candidates_<name>_<date>.md` (table for Stage 2)

**Stage 2 — Schedule optimisation** (`python schedule.py`):
- Reads the candidates table (after user review/editing)
- Fetches Swapcard slot availability via GraphQL API
- Runs maximum-weight bipartite matching (Hungarian algorithm)
- Prints the optimal schedule to stdout

Configuration lives in `.env` (copy `.env.example`). LLM provider and model are
configurable; supported providers: `anthropic`, `gemini`, `openai`.

## If you (Claude in Cursor) are asked to run recommendations manually

You can still drive the retrieval CLI yourself. The embedding cache must exist first:
if `npm run recall` exits with `No embedding cache…` or `cache is stale`, run
`npm run embed` (or `npm run embed -- --force`) and then retry.

### Query construction

From the requester's full profile + user-provided context, produce:

- **query** (2–5 sentence prose paragraph): who the requester would benefit from meeting
  AND who would benefit from meeting *them*. Name sub-fields, role types, problems.
- **wantedKeywords** (15–30 terms): jargon, sub-fields, specific org/product names
  (e.g. `GovAI`, `METR`). Short (1–3 words), de-stopworded. BM25 exact-term hits.
- **lateralKeywords** (12–25 terms): deliberately off-axis — adjacent domains with
  different vocabulary. These power serendipitous picks.

Avoid generic filler ("AI", "EA", "research", "effective", "global", "impact").

### Retrieval

```
npm run recall -- <requester-id> \
  --query   "<prose paragraph>" \
  --wanted  "kw1, kw2, ..." \
  --lateral "kw1, kw2, ..."
```

Then fetch full profiles in one batched call — never loop per id:
```
npm run find -- --id id1,id2,id3,...
```

### Output format

Produce N recommendations (default 25) with **1–5 ratings** (5 = strongest match).
Output sections: `## Rating 5` first, down to `## Rating 1`. No "Core/Adjacent/Wildcard"
labels. Each card: name/role/flag, ~80-word why, 3 talking points, suggested opener,
Swapcard + LinkedIn links.

Diversity: 1 person per org across the whole list (one exception allowed).
Exclusions: requester themselves, obvious teammates, anyone already recommended in
a previous run for this person.

Write output to `output/recommendations_<slug>_<date>.md` and
`output/meeting_candidates_<slug>_<date>.md` (5-column table matching schedule.py input).

## CLI reference

| Command | What it does |
|---|---|
| `npm run find -- "<query>"` | Fuzzy name search |
| `npm run find -- --id <id>` | Full profile for one attendee |
| `npm run find -- --id <id1,id2,id3>` | Batch full profiles — always prefer this |
| `npm run find -- --id <id> --brief` | Header line only |
| `npm run find -- --id <ids> --json` | Raw JSON |
| `npm run find -- --refresh "<query>"` | Bypass 60-min sheet cache |
| `npm run embed` | Build local embedding cache (one-time, a few minutes) |
| `npm run embed -- --force` | Force rebuild |
| `npm run recall -- <id> --query "..." --wanted "a,b,c"` | Hybrid retrieval, primary pool |
| `npm run recall -- <id> --query "..." --wanted "..." --lateral "x,y,z"` | + lateral pool |
| `npm run recall -- <id> ... --top 100 --top-lateral 30` | Custom pool sizes |
| `npm run recall -- <id> ... --json` | JSON output |
| `npm run retrieve -- <id> --wanted "..."` | BM25-only fallback (no embeddings) |

Sheet is re-fetched per Node process (60-min in-process cache only). Always batch
`find --id a,b,c` rather than looping. Override sheet with `SHEET_ID` / `SHEET_GID` env vars.

## Retrieval notes

**Hybrid search** RRF-fuses two rankings: semantic (local `bge-small` embeddings of
`--query` vs cached attendee vectors) + BM25 (`--wanted` terms over bio/expertise/
interests/helpOthers/jobTitle/company). Embeddings close the vocabulary gap; BM25 catches
exact rare tokens (org names). The lateral pool runs a second independent hybrid search
over `--lateral` terms, excluding everyone in the primary pool — this guarantees lateral
picks are genuinely off-axis.

A four-way bake-off found hybrid gives the best hit-rate at ~⅓ the cost of reading every
profile.

**Embedding cache**: flat float32 binary at `cache/embeddings.bin` + `.meta.json`
sidecar. Git-ignored. Invalidated when the sheet changes (`corpusSignature` hash) or the
model changes. Auto-rebuilt by `npm run embed`.
