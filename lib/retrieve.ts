import MiniSearch, { type SearchResult } from "minisearch";
import type { Attendee, Candidate } from "./types";

export type RetrieveQuery = {
  wantedKeywords: string[];
  offeredKeywords: string[];
  lateralKeywords?: string[];
};

export type RetrieveResult = {
  primary: Candidate[];
  lateral: Candidate[];
};

const EXTRA_STOPWORDS = new Set([
  "ai",
  "the",
  "and",
  "or",
  "for",
  "to",
  "of",
  "a",
  "an",
  "in",
  "on",
  "with",
  "by",
  "as",
  "at",
  "is",
  "i",
  "we",
  "ea",
  "effective",
  "altruism",
  "altruist",
  "altruistic",
  "research",
  "researcher",
  "global",
  "impact",
  "work",
  "working",
  "experience",
  "people",
  "person",
  "help",
  "looking",
  "interested",
]);

const processTerm = (term: string): string | null => {
  const lower = term.toLowerCase();
  if (lower.length < 2) return null;
  if (EXTRA_STOPWORDS.has(lower)) return null;
  return lower;
};

type IndexedDoc = {
  id: string;
  fitProfile: string;
  needHelp: string;
};

const toDoc = (a: Attendee): IndexedDoc => ({
  id: a.id,
  fitProfile: [
    a.biography,
    a.expertise.join(" "),
    a.interests.join(" "),
    a.helpOthers,
    a.jobTitle,
    a.company,
  ]
    .filter(Boolean)
    .join("\n"),
  needHelp: a.needHelp,
});

let cache: { signature: string; index: MiniSearch<IndexedDoc> } | null = null;

const signatureFor = (attendees: Attendee[]): string =>
  `${attendees.length}:${attendees[0]?.id ?? ""}:${attendees.at(-1)?.id ?? ""}`;

export function buildIndex(attendees: Attendee[]): MiniSearch<IndexedDoc> {
  const sig = signatureFor(attendees);
  if (cache && cache.signature === sig) return cache.index;
  const index = new MiniSearch<IndexedDoc>({
    fields: ["fitProfile", "needHelp"],
    storeFields: ["id"],
    processTerm,
    searchOptions: {
      combineWith: "OR",
      prefix: true,
      fuzzy: 0.15,
      processTerm,
    },
  });
  index.addAll(attendees.map(toDoc));
  cache = { signature: sig, index };
  return index;
}

const RRF_K = 60;
const WEIGHT_WANTED = 0.7;
const WEIGHT_OFFERED = 0.3;

type Fused = {
  id: string;
  score: number;
  matchedWanted: Set<string>;
  matchedOffered: Set<string>;
};

const fuse = (
  wantedResults: SearchResult[],
  offeredResults: SearchResult[],
): Map<string, Fused> => {
  const merged = new Map<string, Fused>();
  const upsert = (id: string): Fused => {
    let existing = merged.get(id);
    if (!existing) {
      existing = {
        id,
        score: 0,
        matchedWanted: new Set(),
        matchedOffered: new Set(),
      };
      merged.set(id, existing);
    }
    return existing;
  };

  wantedResults.forEach((r, rank) => {
    const f = upsert(r.id as string);
    f.score += WEIGHT_WANTED / (RRF_K + rank + 1);
    for (const q of r.queryTerms) f.matchedWanted.add(q);
  });
  offeredResults.forEach((r, rank) => {
    const f = upsert(r.id as string);
    f.score += WEIGHT_OFFERED / (RRF_K + rank + 1);
    for (const q of r.queryTerms) f.matchedOffered.add(q);
  });
  return merged;
};

const buildQuery = (keywords: string[]): string =>
  keywords
    .map((k) => k.trim())
    .filter(Boolean)
    .join(" ");

export function retrieveCandidates(
  requester: Attendee,
  query: RetrieveQuery,
  attendees: Attendee[],
  topN = 150,
  topLateral = 40,
): RetrieveResult {
  const index = buildIndex(attendees);
  const wantedQuery = buildQuery(query.wantedKeywords);
  const offeredQuery = buildQuery(query.offeredKeywords);
  const lateralQuery = buildQuery(query.lateralKeywords ?? []);

  const wantedResults = wantedQuery
    ? index.search(wantedQuery, { fields: ["fitProfile"] })
    : [];
  const offeredResults = offeredQuery
    ? index.search(offeredQuery, { fields: ["needHelp"] })
    : [];

  const fused = fuse(wantedResults, offeredResults);
  fused.delete(requester.id);

  const byId = new Map(attendees.map((a) => [a.id, a]));
  const primary = [...fused.values()]
    .sort((a, b) => b.score - a.score)
    .map((f) => {
      const a = byId.get(f.id);
      if (!a) return null;
      return {
        ...a,
        matchedWanted: [...f.matchedWanted],
        matchedOffered: [...f.matchedOffered],
        matchedLateral: [] as string[],
        retrievalScore: f.score,
      } satisfies Candidate;
    })
    .filter((x): x is Candidate => x !== null)
    .slice(0, topN);

  // Lateral pool: a separate off-axis search, deliberately excluding the
  // requester AND everyone already in the primary pool, so it surfaces people
  // the similarity ranking would never have shown. This is what makes
  // serendipitous picks real rather than "least-similar of the similar".
  const primaryIds = new Set(primary.map((c) => c.id));
  const lateralResults = lateralQuery
    ? index.search(lateralQuery, { fields: ["fitProfile"] })
    : [];
  const lateral = lateralResults
    .filter((r) => r.id !== requester.id && !primaryIds.has(r.id as string))
    .map((r) => {
      const a = byId.get(r.id as string);
      if (!a) return null;
      return {
        ...a,
        matchedWanted: [] as string[],
        matchedOffered: [] as string[],
        matchedLateral: [...r.queryTerms],
        retrievalScore: r.score,
      } satisfies Candidate;
    })
    .filter((x): x is Candidate => x !== null)
    .slice(0, topLateral);

  return { primary, lateral };
}

export function clearIndexCache() {
  cache = null;
}
