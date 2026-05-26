import { parseArgs } from "node:util";
import { fetchAttendees } from "../lib/sheet";
import {
  EMBED_DIM,
  EMBED_MODEL,
  corpusSignature,
  embedText,
  embedTexts,
  loadCache,
  saveCache,
} from "../lib/embed";

const USAGE = `Usage:
  npm run embed                       Build/refresh the local embedding cache for all attendees
  npm run embed -- --out <path>       Write the cache somewhere other than the default
  npm run embed -- --force            Re-embed even if an up-to-date cache exists

Embeds every attendee profile with a local model (${EMBED_MODEL}, ${EMBED_DIM}-dim)
and caches the vectors to disk. One-time (~minutes); reruns are instant unless the
sheet snapshot changed. No API key, no network beyond the one-time model download.`;

const DEFAULT_OUT = "cache/embeddings.bin";

async function main() {
  const { values } = parseArgs({
    options: {
      out: { type: "string", default: DEFAULT_OUT },
      force: { type: "boolean", default: false },
      refresh: { type: "boolean", default: false },
      help: { type: "boolean", default: false },
    },
  });
  if (values.help) {
    console.log(USAGE);
    return;
  }

  const out = values.out ?? DEFAULT_OUT;
  const attendees = await fetchAttendees({ force: values.refresh });
  const signature = corpusSignature(attendees);

  if (!values.force) {
    const existing = loadCache(out);
    if (existing && existing.signature === signature && existing.model === EMBED_MODEL) {
      console.log(
        `Cache up to date: ${existing.ids.length} vectors at ${out} (signature ${signature}). Use --force to rebuild.`,
      );
      return;
    }
  }

  console.log(`Embedding ${attendees.length} attendees with ${EMBED_MODEL}…`);
  const texts = attendees.map(embedText);
  const t0 = Date.now();
  const vectors = await embedTexts(texts, {
    onProgress: (done, total) => {
      if (done % 256 === 0 || done === total) {
        process.stdout.write(`\r  ${done}/${total}`);
      }
    },
  });
  process.stdout.write("\n");

  saveCache(out, {
    model: EMBED_MODEL,
    dim: EMBED_DIM,
    signature,
    ids: attendees.map((a) => a.id),
    vectors,
  });
  console.log(
    `Wrote ${vectors.length} vectors to ${out} in ${((Date.now() - t0) / 1000).toFixed(1)}s (signature ${signature}).`,
  );
}

main().catch((err) => {
  console.error(err instanceof Error ? err.message : String(err));
  process.exit(1);
});
