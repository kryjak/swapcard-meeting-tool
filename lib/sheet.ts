import { createHash } from "node:crypto";
import Papa from "papaparse";
import type { Attendee } from "./types";

const SHEET_ID =
  process.env.SHEET_ID ?? "1Uvj0sDZzQJN0gUsccpw0HAf5qjFB6pkbjGMt2QuH6RA";
const SHEET_GID = process.env.SHEET_GID ?? "43679916";

const CSV_URL = `https://docs.google.com/spreadsheets/d/${SHEET_ID}/export?format=csv&gid=${SHEET_GID}`;

const TTL_MS = 60 * 60 * 1000;
let cache: { fetchedAt: number; attendees: Attendee[] } | null = null;

const HEADER_FIRST_CELL = "First Name";

const splitList = (s: string | undefined): string[] => {
  if (!s) return [];
  return s
    .split(/;|\n/)
    .map((x) => x.trim())
    .filter(Boolean);
};

const slugify = (s: string): string =>
  s
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-|-$/g, "");

const stableHash = (s: string): string =>
  createHash("sha1").update(s).digest("hex").slice(0, 10);

const swapcardToken = (url: string, fallback: string): string => {
  const m = url.match(/person\/([^/?#]+)/);
  return stableHash(m ? m[1] : fallback);
};

const isAttendeePresent = (a: Attendee): boolean =>
  !!(a.firstName && a.lastName) && hasAnyContent(a);

const hasAnyContent = (a: Attendee): boolean =>
  !!(
    a.biography ||
    a.expertise.length ||
    a.interests.length ||
    a.helpOthers ||
    a.needHelp ||
    a.jobTitle ||
    a.company
  );

export async function fetchAttendees(opts?: {
  force?: boolean;
}): Promise<Attendee[]> {
  if (!opts?.force && cache && Date.now() - cache.fetchedAt < TTL_MS) {
    return cache.attendees;
  }
  const res = await fetch(CSV_URL);
  if (!res.ok) {
    throw new Error(`Sheet fetch failed: ${res.status}`);
  }
  const text = await res.text();
  const parsed = Papa.parse<string[]>(text, { skipEmptyLines: false });
  if (parsed.errors.length) {
    // Don't bail — papaparse reports stray field errors but recovers
    console.warn(`Sheet parse warnings: ${parsed.errors.length}`);
  }
  const rows = parsed.data;
  const headerIdx = rows.findIndex(
    (r) => Array.isArray(r) && r[0]?.trim() === HEADER_FIRST_CELL,
  );
  if (headerIdx < 0) {
    throw new Error("Could not find header row in sheet");
  }
  const dataRows = rows.slice(headerIdx + 1);
  const attendees: Attendee[] = [];
  for (const row of dataRows) {
    if (!Array.isArray(row) || row.every((c) => !c?.trim())) continue;
    const a: Attendee = {
      id: "",
      firstName: row[0]?.trim() ?? "",
      lastName: row[1]?.trim() ?? "",
      company: row[2]?.trim() ?? "",
      jobTitle: row[3]?.trim() ?? "",
      careerStage: row[4]?.trim() ?? "",
      biography: row[5]?.trim() ?? "",
      expertise: splitList(row[6]),
      interests: splitList(row[7]),
      needHelp: row[8]?.trim() ?? "",
      helpOthers: row[9]?.trim() ?? "",
      country: row[10]?.trim() ?? "",
      seekingWork: row[11]?.trim() ?? "",
      recruitment: splitList(row[12]),
      swapcardUrl: row[13]?.trim() ?? "",
      linkedinUrl: row[14]?.trim() ?? "",
    };
    if (!isAttendeePresent(a)) continue;
    const fallback = `${a.firstName}|${a.lastName}|${a.linkedinUrl}|${a.company}`;
    a.id = `${slugify(`${a.firstName} ${a.lastName}`)}-${swapcardToken(a.swapcardUrl, fallback)}`;
    attendees.push(a);
  }
  const deduped = dedupeById(attendees);
  cache = { fetchedAt: Date.now(), attendees: deduped };
  return deduped;
}

const richness = (a: Attendee): number =>
  (a.biography?.length ?? 0) +
  (a.needHelp?.length ?? 0) +
  (a.helpOthers?.length ?? 0) +
  a.expertise.length * 10 +
  a.interests.length * 10;

const dedupeById = (attendees: Attendee[]): Attendee[] => {
  const byId = new Map<string, Attendee>();
  for (const a of attendees) {
    const existing = byId.get(a.id);
    if (!existing || richness(a) > richness(existing)) {
      byId.set(a.id, a);
    }
  }
  return [...byId.values()];
};

export function nonEmptyAttendees(all: Attendee[]): Attendee[] {
  return all.filter(hasAnyContent);
}

export function findAttendee(
  all: Attendee[],
  id: string,
): Attendee | undefined {
  return all.find((a) => a.id === id);
}

export function searchByName(all: Attendee[], q: string, limit = 8): Attendee[] {
  const needle = q.trim().toLowerCase();
  if (!needle) return [];
  const tokens = needle.split(/\s+/);
  const scored: { a: Attendee; score: number }[] = [];
  for (const a of all) {
    const hay = `${a.firstName} ${a.lastName}`.toLowerCase();
    let score = 0;
    for (const t of tokens) {
      if (hay.startsWith(t)) score += 3;
      else if (hay.includes(` ${t}`)) score += 2;
      else if (hay.includes(t)) score += 1;
      else {
        score = -1;
        break;
      }
    }
    if (score > 0) scored.push({ a, score });
  }
  scored.sort((x, y) => y.score - x.score);
  return scored.slice(0, limit).map((s) => s.a);
}

export function clearCache() {
  cache = null;
}
