import { parseArgs } from "node:util";
import { fetchAttendees, findAttendee } from "../lib/sheet";
import { retrieveCandidates } from "../lib/retrieve";
import type { Candidate } from "../lib/types";

const USAGE = `Usage:
  npm run retrieve -- <requester-id> \\
    --wanted  "kw1, kw2, kw3, ..."   (comma-separated; what the requester is looking for)
    --offered "kw1, kw2, kw3, ..."   (comma-separated; what the requester offers)
    [--lateral "kw1, kw2, ..."]       (adjacent/off-axis domains; powers serendipity picks)
    [--top 150]                       (primary shortlist size, default 150)
    [--top-lateral 40]                (lateral pool size, default 40)
    [--json]                          (raw JSON; default is readable text)
    [--refresh]                       (bypass sheet cache)

Prints a compact, readable ranked list by default — small enough to read
directly, no JSON parsing needed. Pass --json for the full machine object.
candidates are the similarity-ranked shortlist (matchedWanted/matchedOffered).
lateralCandidates is a SEPARATE off-axis pool (matchedLateral) — people the
similarity ranking would never surface, for drawing serendipitous picks from.
Then fetch full profiles for your shortlist in ONE call:
  npm run find -- --id id1,id2,id3,...`;

const splitKeywords = (s: string | undefined): string[] => {
  if (!s) return [];
  return s
    .split(/[,\n]/)
    .map((x) => x.trim())
    .filter(Boolean);
};

const candidateView = (c: Candidate, brief: boolean) => {
  if (!brief) return c;
  return {
    id: c.id,
    name: `${c.firstName} ${c.lastName}`.trim(),
    jobTitle: c.jobTitle,
    company: c.company,
    country: c.country,
    matchedWanted: c.matchedWanted,
    matchedOffered: c.matchedOffered,
    matchedLateral: c.matchedLateral,
    retrievalScore: Number(c.retrievalScore.toFixed(4)),
  };
};

const kw = (xs: string[] | undefined): string => (xs && xs.length ? xs.join(", ") : "—");

// One readable line-group per candidate; `keys` are the matched-keyword fields to show.
const candidateLines = (c: Candidate, i: number, keys: (keyof Candidate)[]): string => {
  const name = `${c.firstName} ${c.lastName}`.trim();
  const head = `${i + 1}. ${name} — ${c.jobTitle || "?"}, ${c.company || "?"} (${c.country || "?"})  [${c.id}]  score=${c.retrievalScore.toFixed(4)}`;
  const matched = keys
    .map((k) => [k, kw(c[k] as string[] | undefined)] as const)
    .filter(([, v]) => v !== "—")
    .map(([k, v]) => `   ${String(k).replace("matched", "").toLowerCase()}: ${v}`);
  return [head, ...matched].join("\n");
};

const renderText = (
  requester: { id: string; name: string },
  candidates: Candidate[],
  lateral: Candidate[],
): string => {
  const out = [
    `Requester: ${requester.name} [${requester.id}]`,
    `Pools: ${candidates.length} candidates (similarity-ranked), ${lateral.length} lateral (off-axis)`,
    "",
    "=== CANDIDATES (Core/Adjacent picks) ===",
    ...candidates.map((c, i) => candidateLines(c, i, ["matchedWanted", "matchedOffered"])),
    "",
    "=== LATERAL POOL (Wildcard picks) ===",
    ...lateral.map((c, i) => candidateLines(c, i, ["matchedLateral"])),
  ];
  return out.join("\n");
};

async function main() {
  const { values, positionals } = parseArgs({
    allowPositionals: true,
    options: {
      wanted: { type: "string" },
      offered: { type: "string" },
      lateral: { type: "string" },
      top: { type: "string", default: "150" },
      "top-lateral": { type: "string", default: "40" },
      brief: { type: "boolean", default: false },
      json: { type: "boolean", default: false },
      refresh: { type: "boolean", default: false },
      help: { type: "boolean", default: false },
    },
  });

  if (values.help || positionals.length === 0) {
    console.log(USAGE);
    if (!values.help) process.exit(1);
    return;
  }

  const requesterId = positionals[0];
  const wantedKeywords = splitKeywords(values.wanted);
  const offeredKeywords = splitKeywords(values.offered);
  const lateralKeywords = splitKeywords(values.lateral);

  if (wantedKeywords.length === 0 && offeredKeywords.length === 0) {
    console.error("Provide at least one of --wanted or --offered.\n");
    console.error(USAGE);
    process.exit(1);
  }

  const top = Number.parseInt(values.top ?? "150", 10);
  const topLateral = Number.parseInt(values["top-lateral"] ?? "40", 10);
  const attendees = await fetchAttendees({ force: values.refresh });
  const requester = findAttendee(attendees, requesterId);
  if (!requester) {
    console.error(`No attendee with id: ${requesterId}`);
    process.exit(2);
  }

  const { primary, lateral } = retrieveCandidates(
    requester,
    { wantedKeywords, offeredKeywords, lateralKeywords },
    attendees,
    Number.isFinite(top) ? top : 150,
    Number.isFinite(topLateral) ? topLateral : 40,
  );

  const requesterView = {
    id: requester.id,
    name: `${requester.firstName} ${requester.lastName}`.trim(),
  };

  if (!values.json) {
    console.log(renderText(requesterView, primary, lateral));
    return;
  }

  const out = {
    requester: requesterView,
    query: { wantedKeywords, offeredKeywords, lateralKeywords },
    totalAttendees: attendees.length,
    candidateCount: primary.length,
    lateralCount: lateral.length,
    candidates: primary.map((c) => candidateView(c, values.brief)),
    lateralCandidates: lateral.map((c) => candidateView(c, values.brief)),
  };

  console.log(JSON.stringify(out, null, 2));
}

main().catch((err) => {
  console.error(err instanceof Error ? err.message : String(err));
  process.exit(1);
});
