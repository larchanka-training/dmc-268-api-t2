#!/usr/bin/env node
/**
 * Catalog sync checksum for `.agents/skills/**`.
 *
 * Algorithm (this file is the source of truth):
 * 1. Walk every regular file under `<repo>/.agents/skills` (recursive).
 * 2. For each file, emit one line: `<posix-relpath> <sha256-hex-of-bytes>`
 *    where `<posix-relpath>` is relative to `.agents/skills` with `/` separators.
 * 3. Sort lines lexicographically (UTF-16 code units / JS default string sort).
 * 4. Join with `\n` and append a trailing `\n`.
 * 5. SHA-256 hex digest of that UTF-8 string is the catalog sync SHA.
 *
 * Usage (from repo root):
 *   node .agents/check-sync.mjs           # verify vs .agents/SYNC_SHA.txt (0=match, 1=mismatch)
 *   node .agents/check-sync.mjs --write   # write .agents/SYNC_SHA.txt
 */
import { createHash } from "node:crypto";
import { readdirSync, readFileSync, statSync, writeFileSync, existsSync } from "node:fs";
import { dirname, join, relative, sep } from "node:path";
import { fileURLToPath } from "node:url";

const agentsDir = dirname(fileURLToPath(import.meta.url));
const skillsRoot = join(agentsDir, "skills");
const shaPath = join(agentsDir, "SYNC_SHA.txt");

function walkFiles(dir) {
  const out = [];
  for (const name of readdirSync(dir)) {
    const abs = join(dir, name);
    const st = statSync(abs);
    if (st.isDirectory()) out.push(...walkFiles(abs));
    else if (st.isFile()) out.push(abs);
  }
  return out;
}

function toPosixRel(abs, root) {
  return relative(root, abs).split(sep).join("/");
}

function computeSyncSha(root = skillsRoot) {
  if (!existsSync(root)) {
    throw new Error(`skills root missing: ${root}`);
  }
  const lines = walkFiles(root)
    .map((abs) => {
      const rel = toPosixRel(abs, root);
      const digest = createHash("sha256").update(readFileSync(abs)).digest("hex");
      return `${rel} ${digest}`;
    })
    .sort();
  return createHash("sha256").update(lines.join("\n") + "\n").digest("hex");
}

function main() {
  const write = process.argv.includes("--write");
  // Allow tests / callers to set cwd as repo root: resolve skills relative to cwd if present.
  const cwdSkills = join(process.cwd(), ".agents", "skills");
  const cwdSha = join(process.cwd(), ".agents", "SYNC_SHA.txt");
  const useCwd = existsSync(cwdSkills);
  const root = useCwd ? cwdSkills : skillsRoot;
  const outPath = useCwd ? cwdSha : shaPath;

  const sha = computeSyncSha(root);

  if (write) {
    writeFileSync(outPath, sha + "\n", "utf8");
    process.stdout.write(`${sha}\n`);
    process.exit(0);
  }

  if (!existsSync(outPath)) {
    process.stderr.write(`missing ${outPath}; run with --write\n`);
    process.stdout.write(`${sha}\n`);
    process.exit(1);
  }

  const expected = readFileSync(outPath, "utf8").trim();
  process.stdout.write(`${sha}\n`);
  if (sha === expected) {
    process.exit(0);
  }
  process.stderr.write(
    `SYNC_SHA mismatch: computed ${sha}, committed ${expected}\n`,
  );
  process.exit(1);
}

main();
