import { parseArgs } from "node:util";
import { fetchAttendees, findAttendee, searchByName } from "../lib/sheet";
import type { Attendee } from "../lib/types";

const USAGE = `Usage:
  npm run find -- "<name query>"           Search attendees by name (top matches)
  npm run find -- --id <attendee-id>       Print the full attendee profile by id
  npm run find -- --id <id1,id2,id3>       Print full profiles for several ids at once
  npm run find -- --id <id> --brief        Print just the summary fields
  npm run find -- --refresh "<query>"      Bypass the 60-min sheet cache
  npm run find -- --id <ids> --json        Raw JSON instead of readable text

Prints readable text by default (no JSON parsing needed). Pass --json for the
raw object/array. Always batch multiple ids in ONE call (--id a,b,c) — the sheet
cache is per-process, so separate calls re-download the whole sheet each time.`;

const briefView = (a: Attendee) => ({
  id: a.id,
  name: `${a.firstName} ${a.lastName}`.trim(),
  jobTitle: a.jobTitle,
  company: a.company,
  country: a.country,
  careerStage: a.careerStage,
});

const header = (a: Attendee): string =>
  `${a.firstName} ${a.lastName}`.trim() +
  ` — ${a.jobTitle || "?"}, ${a.company || "?"} (${a.country || "?"})  [${a.id}]`;

const list = (xs: string[] | undefined): string => (xs && xs.length ? xs.join(", ") : "—");

// Readable profile block — everything a recommender needs, no JSON parsing.
const profileText = (a: Attendee): string => {
  const lines = [
    `=== ${header(a)}`,
    `Career: ${a.careerStage || "—"}`,
    `Seeking: ${a.seekingWork || "—"}`,
    `Expertise: ${list(a.expertise)}`,
    `Interests: ${list(a.interests)}`,
    `Bio: ${(a.biography || "—").replace(/\s+/g, " ").trim()}`,
    `Need help: ${(a.needHelp || "—").replace(/\s+/g, " ").trim()}`,
    `Help others: ${(a.helpOthers || "—").replace(/\s+/g, " ").trim()}`,
    `Swapcard: ${a.swapcardUrl || "— (search by name)"}`,
    `LinkedIn: ${a.linkedinUrl || "—"}`,
  ];
  return lines.join("\n");
};

async function main() {
  const { values, positionals } = parseArgs({
    allowPositionals: true,
    options: {
      id: { type: "string" },
      brief: { type: "boolean", default: false },
      json: { type: "boolean", default: false },
      limit: { type: "string", default: "10" },
      refresh: { type: "boolean", default: false },
      help: { type: "boolean", default: false },
    },
  });

  if (values.help) {
    console.log(USAGE);
    return;
  }

  const attendees = await fetchAttendees({ force: values.refresh });

  if (values.id) {
    const ids = values.id
      .split(",")
      .map((x) => x.trim())
      .filter(Boolean);
    const view = (a: Attendee) => (values.brief ? briefView(a) : a);
    const missing: string[] = [];
    const found = ids
      .map((id) => {
        const a = findAttendee(attendees, id);
        if (!a) missing.push(id);
        return a;
      })
      .filter((a): a is Attendee => Boolean(a));
    if (found.length === 0) {
      console.error(`No attendee with id: ${missing.join(", ")}`);
      process.exit(2);
    }
    if (missing.length > 0) {
      console.error(`Warning: no attendee with id: ${missing.join(", ")}`);
    }
    if (values.json) {
      // Single id keeps the original object output; multiple ids print an array.
      const payload = ids.length === 1 ? view(found[0]) : found.map(view);
      console.log(JSON.stringify(payload, null, 2));
      return;
    }
    const render = values.brief ? (a: Attendee) => header(a) : profileText;
    console.log(found.map(render).join("\n\n"));
    return;
  }

  const query = positionals.join(" ").trim();
  if (!query) {
    console.error(USAGE);
    process.exit(1);
  }

  const limit = Number.parseInt(values.limit ?? "10", 10);
  const matches = searchByName(attendees, query, Number.isFinite(limit) ? limit : 10);
  if (matches.length === 0) {
    console.error(`No matches for "${query}"`);
    process.exit(3);
  }
  if (values.json) {
    console.log(JSON.stringify(matches.map(briefView), null, 2));
    return;
  }
  console.log(matches.map((a, i) => `${i + 1}. ${header(a)}`).join("\n"));
}

main().catch((err) => {
  console.error(err instanceof Error ? err.message : String(err));
  process.exit(1);
});
