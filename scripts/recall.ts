import { parseArgs } from "node:util";
import type { SearchResult } from "minisearch";
import { fetchAttendees, findAttendee } from "../lib/sheet";
import { buildIndex } from "../lib/retrieve";
import {
  EMBED_MODEL,
  corpusSignature,
  cosine,
  embedOne,
  loadCache,
} from "../lib/embed";
import type { Attendee } from "../lib/types";

const USAGE = `Usage:
  npm run recall -- <requester-id> --query "<prose description of who you're looking for>" \\
    [--wanted  "kw1, kw2, ..."]   (exact-term keywords fused into the PRIMARY pool via BM25)
    [--lateral "kw1, kw2, ..."]   (off-axis terms → a SEPARATE serendipity pool for Wildcard picks)
    [--top 150]                   (primary shortlist size, default 150)
    [--top-lateral 40]            (lateral pool size, default 40)
    [--cache cache/embeddings.bin]
    [--ids]                       (print just the primary ids, comma-separated — pipe into find)
    [--json]

The HYBRID retrieval stage (the production default). Ranks every attendee by
SEMANTIC similarity (local embeddings) to your prose --query, fused (RRF) with a
BM25 search over your --wanted keywords. Embeddings close the vocabulary gap BM25
can't — "scouting researchers" matches "talent identification" with no shared
words. --lateral runs a SECOND, off-axis hybrid search (its own semantic + BM25),
excludes everyone already in the primary pool, and returns them separately — this
is the serendipity engine the Wildcard tier draws from. Run \`npm run embed\` first.`;

const RRF_K = 60;

const splitKeywords = (s: string | undefined): string[] =>
  (s ?? "")
    .split(/[,\n]/)
    .map((x) => x.trim())
    .filter(Boolean);

const header = (a: Attendee): string =>
  `${a.firstName} ${a.lastName}`.trim() +
  ` — ${a.jobTitle || "?"}, ${a.company || "?"} (${a.country || "?"})  [${a.id}]`;

type Scored = { id: string; semRank?: number; bm25Rank?: number; score: number };

// RRF-fuse a semantic ranking (id→rank) with a BM25 ranking (SearchResult[]),
// dropping any id in `exclude`, and return the top `n`.
function fuse(
  semRanked: { id: string; sim: number }[],
  bm25: SearchResult[],
  exclude: Set<string>,
  n: number,
): Scored[] {
  const semRank = new Map(semRanked.map((x, i) => [x.id, i]));
  const bm25Rank = new Map(bm25.map((r, i) => [r.id as string, i]));
  const ids = new Set<string>([...semRank.keys(), ...bm25Rank.keys()]);
  const out: Scored[] = [];
  for (const id of ids) {
    if (exclude.has(id)) continue;
    const sr = semRank.get(id);
    const br = bm25Rank.get(id);
    let score = 0;
    if (sr !== undefined) score += 1 / (RRF_K + sr + 1);
    if (br !== undefined) score += 1 / (RRF_K + br + 1);
    out.push({ id, semRank: sr, bm25Rank: br, score });
  }
  out.sort((a, b) => b.score - a.score);
  return out.slice(0, n);
}

const chan = (s: Scored): string =>
  s.semRank !== undefined && s.bm25Rank !== undefined
    ? `sem#${s.semRank + 1}/bm25#${s.bm25Rank + 1}`
    : s.semRank !== undefined
      ? `sem#${s.semRank + 1}`
      : `bm25#${s.bm25Rank! + 1}`;

async function main() {
  const { values, positionals } = parseArgs({
    allowPositionals: true,
    options: {
      query: { type: "string" },
      wanted: { type: "string" },
      lateral: { type: "string" },
      top: { type: "string", default: "150" },
      "top-lateral": { type: "string", default: "40" },
      cache: { type: "string", default: "cache/embeddings.bin" },
      ids: { type: "boolean", default: false },
      json: { type: "boolean", default: false },
      refresh: { type: "boolean", default: false },
      help: { type: "boolean", default: false },
    },
  });

  if (values.help || positionals.length === 0 || !values.query) {
    console.log(USAGE);
    if (!values.help) process.exit(1);
    return;
  }

  const requesterId = positionals[0];
  const top = Number.parseInt(values.top ?? "150", 10) || 150;
  const topLateral = Number.parseInt(values["top-lateral"] ?? "40", 10) || 40;
  const attendees = await fetchAttendees({ force: values.refresh });
  const requester = findAttendee(attendees, requesterId);
  if (!requester) {
    console.error(`No attendee with id: ${requesterId}`);
    process.exit(2);
  }

  const cache = loadCache(values.cache ?? "cache/embeddings.bin");
  if (!cache) {
    console.error(`No embedding cache at ${values.cache}. Build it first:  npm run embed`);
    process.exit(2);
  }
  if (cache.signature !== corpusSignature(attendees) || cache.model !== EMBED_MODEL) {
    console.error(`Embedding cache is stale (sheet snapshot changed). Rebuild:  npm run embed -- --force`);
    process.exit(2);
  }

  const index = buildIndex(attendees);
  const semRankFor = (queryVec: Float32Array) =>
    cache.ids
      .map((id, i) => ({ id, sim: cosine(queryVec, cache.vectors[i]) }))
      .sort((a, b) => b.sim - a.sim);

  // --- Primary pool: semantic(query) RRF BM25(wanted) ---
  const qvec = await embedOne(values.query);
  const wanted = splitKeywords(values.wanted);
  const primary = fuse(
    semRankFor(qvec),
    wanted.length ? index.search(wanted.join(" "), { fields: ["fitProfile"] }) : [],
    new Set([requesterId]),
    top,
  );

  // --- Lateral pool: off-axis semantic + BM25, excluding the primary pool ---
  const lateralKw = splitKeywords(values.lateral);
  let lateral: Scored[] = [];
  if (lateralKw.length) {
    const exclude = new Set<string>([requesterId, ...primary.map((p) => p.id)]);
    const latVec = await embedOne(lateralKw.join(", "));
    lateral = fuse(
      semRankFor(latVec),
      index.search(lateralKw.join(" "), { fields: ["fitProfile"] }),
      exclude,
      topLateral,
    );
  }

  const byId = new Map(attendees.map((a) => [a.id, a]));

  if (values.ids) {
    console.log(primary.map((s) => s.id).join(","));
    return;
  }
  if (values.json) {
    const view = (s: Scored) => {
      const a = byId.get(s.id)!;
      return {
        id: s.id,
        name: `${a.firstName} ${a.lastName}`.trim(),
        jobTitle: a.jobTitle,
        company: a.company,
        country: a.country,
        semRank: s.semRank,
        bm25Rank: s.bm25Rank,
        score: Number(s.score.toFixed(5)),
      };
    };
    console.log(
      JSON.stringify(
        {
          requester: { id: requester.id, name: `${requester.firstName} ${requester.lastName}`.trim() },
          query: values.query,
          wanted,
          lateral: lateralKw,
          totalAttendees: attendees.length,
          candidates: primary.map(view),
          lateralCandidates: lateral.map(view),
        },
        null,
        2,
      ),
    );
    return;
  }

  console.log(`Requester: ${requester.firstName} ${requester.lastName} [${requester.id}]`);
  console.log(
    `Pools: ${primary.length} candidates (hybrid: semantic${wanted.length ? " + BM25" : ""}), ${lateral.length} lateral (off-axis)`,
  );
  console.log("");
  console.log("=== CANDIDATES (Core/Adjacent picks) ===");
  primary.forEach((s, i) => console.log(`${i + 1}. ${header(byId.get(s.id)!)}  (${chan(s)})`));
  if (lateral.length) {
    console.log("");
    console.log("=== LATERAL POOL (Wildcard picks) ===");
    lateral.forEach((s, i) => console.log(`${i + 1}. ${header(byId.get(s.id)!)}  (${chan(s)})`));
  }
}

main().catch((err) => {
  console.error(err instanceof Error ? err.message : String(err));
  process.exit(1);
});
