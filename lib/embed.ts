import { createHash } from "node:crypto";
import { mkdirSync, readFileSync, writeFileSync, existsSync } from "node:fs";
import { dirname } from "node:path";
import type { Attendee } from "./types";

// Local sentence-embedding model — runs on-device via transformers.js, no API
// key, no per-use cost. 384-dim, normalised, mean-pooled. This is the semantic
// channel that closes the vocabulary gap BM25 leaves open.
export const EMBED_MODEL = "Xenova/bge-small-en-v1.5";
export const EMBED_DIM = 384;

// The text we embed per attendee — same fields BM25 indexes, joined as prose so
// the model sees natural language rather than a keyword bag.
export const embedText = (a: Attendee): string =>
  [
    `${a.firstName} ${a.lastName}`,
    a.jobTitle,
    a.company,
    a.country,
    a.careerStage,
    a.biography,
    a.expertise.join(", "),
    a.interests.join(", "),
    a.helpOthers ? `Can help others with: ${a.helpOthers}` : "",
    a.needHelp ? `Needs help with: ${a.needHelp}` : "",
  ]
    .filter(Boolean)
    .join("\n");

let extractorPromise: Promise<any> | null = null;
async function getExtractor() {
  if (!extractorPromise) {
    const { pipeline, env } = await import("@xenova/transformers");
    // Keep model + cache local and quiet.
    env.allowRemoteModels = true;
    extractorPromise = pipeline("feature-extraction", EMBED_MODEL);
  }
  return extractorPromise;
}

export async function embedTexts(
  texts: string[],
  opts?: { batchSize?: number; onProgress?: (done: number, total: number) => void },
): Promise<Float32Array[]> {
  const extractor = await getExtractor();
  const batchSize = opts?.batchSize ?? 32;
  const out: Float32Array[] = [];
  for (let i = 0; i < texts.length; i += batchSize) {
    const batch = texts.slice(i, i + batchSize);
    const res = await extractor(batch, { pooling: "mean", normalize: true });
    const rows = res.tolist() as number[][];
    for (const r of rows) out.push(Float32Array.from(r));
    opts?.onProgress?.(Math.min(i + batchSize, texts.length), texts.length);
  }
  return out;
}

export async function embedOne(text: string): Promise<Float32Array> {
  const [v] = await embedTexts([text]);
  return v;
}

export const cosine = (a: Float32Array, b: Float32Array): number => {
  // Vectors are pre-normalised, so dot product == cosine similarity.
  let s = 0;
  for (let i = 0; i < a.length; i++) s += a[i] * b[i];
  return s;
};

// ---- On-disk cache: a flat Float32 blob + a sidecar of ids/signature. ----

export type EmbedCache = {
  model: string;
  dim: number;
  signature: string; // ties the cache to a specific sheet snapshot
  ids: string[];
  vectors: Float32Array[];
};

const metaPath = (binPath: string) => binPath.replace(/\.bin$/, ".meta.json");

export function corpusSignature(attendees: Attendee[]): string {
  const h = createHash("sha1");
  for (const a of attendees) h.update(a.id).update("\n");
  return `${attendees.length}:${h.digest("hex").slice(0, 12)}`;
}

export function saveCache(binPath: string, cache: EmbedCache): void {
  mkdirSync(dirname(binPath), { recursive: true });
  const buf = Buffer.alloc(cache.vectors.length * cache.dim * 4);
  let off = 0;
  for (const v of cache.vectors) {
    for (let i = 0; i < cache.dim; i++) {
      buf.writeFloatLE(v[i], off);
      off += 4;
    }
  }
  writeFileSync(binPath, buf);
  writeFileSync(
    metaPath(binPath),
    JSON.stringify(
      { model: cache.model, dim: cache.dim, signature: cache.signature, ids: cache.ids },
      null,
      2,
    ),
  );
}

export function loadCache(binPath: string): EmbedCache | null {
  if (!existsSync(binPath) || !existsSync(metaPath(binPath))) return null;
  const meta = JSON.parse(readFileSync(metaPath(binPath), "utf8")) as {
    model: string;
    dim: number;
    signature: string;
    ids: string[];
  };
  const buf = readFileSync(binPath);
  const vectors: Float32Array[] = [];
  let off = 0;
  for (let n = 0; n < meta.ids.length; n++) {
    const v = new Float32Array(meta.dim);
    for (let i = 0; i < meta.dim; i++) {
      v[i] = buf.readFloatLE(off);
      off += 4;
    }
    vectors.push(v);
  }
  return { ...meta, vectors };
}
