import { parseArgs } from "node:util";
import { mkdirSync, writeFileSync, readFileSync, existsSync } from "node:fs";
import { fetchAttendees, findAttendee } from "../lib/sheet";
import type { Attendee } from "../lib/types";

const USAGE = `Usage:
  npm run dump                              Write ALL attendee profiles, batched, to cache/batches/
  npm run dump -- --batch-size 250          Profiles per batch file (default 250)
  npm run dump -- --out-dir <dir>           Where to write batch files (default cache/batches)
  npm run dump -- --ids id1,id2,...         Only dump these ids (e.g. a hybrid recall shortlist), still batched
  npm run dump -- --ids-file <path>         Read a comma/newline-separated id list from a file
  npm run dump -- --exclude id1,id2         Ids to omit (e.g. the requester)
  npm run dump -- --stdout                  Print to stdout instead of writing files

Produces the readable full-profile text the full-read runs feed to subagents.
Each batch file is self-contained: hand one file to one subagent. Writes a
manifest.json listing the batch files and the ids in each.`;

const list = (xs: string[] | undefined): string => (xs && xs.length ? xs.join(", ") : "—");

const profileText = (a: Attendee): string =>
  [
    `### ${a.firstName} ${a.lastName} — ${a.jobTitle || "?"}, ${a.company || "?"} (${a.country || "?"})`,
    `id: ${a.id}`,
    `Career stage: ${a.careerStage || "—"}`,
    `Seeking work: ${a.seekingWork || "—"}`,
    `Expertise: ${list(a.expertise)}`,
    `Interests: ${list(a.interests)}`,
    `Bio: ${(a.biography || "—").replace(/\s+/g, " ").trim()}`,
    `Need help: ${(a.needHelp || "—").replace(/\s+/g, " ").trim()}`,
    `Help others: ${(a.helpOthers || "—").replace(/\s+/g, " ").trim()}`,
    `Swapcard: ${a.swapcardUrl || "—"}  | LinkedIn: ${a.linkedinUrl || "—"}`,
  ].join("\n");

const parseIds = (s: string | undefined): string[] =>
  (s ?? "")
    .split(/[,\n]/)
    .map((x) => x.trim())
    .filter(Boolean);

async function main() {
  const { values } = parseArgs({
    options: {
      "batch-size": { type: "string", default: "250" },
      "out-dir": { type: "string", default: "cache/batches" },
      ids: { type: "string" },
      "ids-file": { type: "string" },
      exclude: { type: "string" },
      stdout: { type: "boolean", default: false },
      refresh: { type: "boolean", default: false },
      help: { type: "boolean", default: false },
    },
  });
  if (values.help) {
    console.log(USAGE);
    return;
  }

  const attendees = await fetchAttendees({ force: values.refresh });
  const byId = new Map(attendees.map((a) => [a.id, a]));

  let idList = parseIds(values.ids);
  if (values["ids-file"]) {
    if (!existsSync(values["ids-file"])) {
      console.error(`ids-file not found: ${values["ids-file"]}`);
      process.exit(2);
    }
    idList = idList.concat(parseIds(readFileSync(values["ids-file"], "utf8")));
  }
  const exclude = new Set(parseIds(values.exclude));

  let selected: Attendee[];
  if (idList.length) {
    selected = idList
      .map((id) => byId.get(id))
      .filter((a): a is Attendee => Boolean(a))
      .filter((a) => !exclude.has(a.id));
  } else {
    selected = attendees.filter((a) => !exclude.has(a.id));
  }

  if (values.stdout) {
    console.log(selected.map(profileText).join("\n\n"));
    return;
  }

  const batchSize = Number.parseInt(values["batch-size"] ?? "250", 10) || 250;
  const outDir = values["out-dir"] ?? "cache/batches";
  mkdirSync(outDir, { recursive: true });

  const manifest: { batch: string; count: number; ids: string[] }[] = [];
  let batchNo = 0;
  for (let i = 0; i < selected.length; i += batchSize) {
    batchNo++;
    const batch = selected.slice(i, i + batchSize);
    const name = `batch-${String(batchNo).padStart(2, "0")}.md`;
    const body = [
      `# Attendee batch ${batchNo} — ${batch.length} profiles (of ${selected.length} total)`,
      "",
      batch.map(profileText).join("\n\n"),
      "",
    ].join("\n");
    writeFileSync(`${outDir}/${name}`, body);
    manifest.push({ batch: name, count: batch.length, ids: batch.map((a) => a.id) });
  }
  writeFileSync(
    `${outDir}/manifest.json`,
    JSON.stringify({ total: selected.length, batchSize, batches: manifest }, null, 2),
  );

  console.log(`Wrote ${batchNo} batch files (${selected.length} profiles) to ${outDir}/`);
  console.log(`Manifest: ${outDir}/manifest.json`);
  for (const m of manifest) console.log(`  ${m.batch}: ${m.count}`);
}

main().catch((err) => {
  console.error(err instanceof Error ? err.message : String(err));
  process.exit(1);
});
