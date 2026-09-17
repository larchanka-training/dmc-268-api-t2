import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdirSync, mkdtempSync, writeFileSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { spawnSync } from "node:child_process";
import test from "node:test";
import { fileURLToPath } from "node:url";

const scriptPath = fileURLToPath(new URL("./check-sync.mjs", import.meta.url));

function makeTree(files) {
  const root = mkdtempSync(join(tmpdir(), "agents-sync-"));
  const skills = join(root, ".agents", "skills");
  for (const [rel, body] of Object.entries(files)) {
    const abs = join(skills, rel);
    mkdirSync(join(abs, ".."), { recursive: true });
    writeFileSync(abs, body);
  }
  return root;
}

function run(cwd, args = []) {
  return spawnSync(process.execPath, [scriptPath, ...args], {
    cwd,
    encoding: "utf8",
  });
}

function expectedSha(files) {
  const lines = Object.entries(files)
    .map(([rel, body]) => {
      const norm = rel.replaceAll("\\", "/");
      const digest = createHash("sha256").update(body).digest("hex");
      return `${norm} ${digest}`;
    })
    .sort();
  return createHash("sha256").update(lines.join("\n") + "\n").digest("hex");
}

test("same skills tree yields the same SHA", () => {
  const files = {
    "a/SKILL.md": "alpha\n",
    "b/nested/note.md": "beta\n",
  };
  const root1 = makeTree(files);
  const root2 = makeTree(files);
  writeFileSync(join(root1, ".agents", "SYNC_SHA.txt"), expectedSha(files) + "\n");
  writeFileSync(join(root2, ".agents", "SYNC_SHA.txt"), expectedSha(files) + "\n");

  const r1 = run(root1);
  const r2 = run(root2);
  assert.equal(r1.status, 0, r1.stderr || r1.stdout);
  assert.equal(r2.status, 0, r2.stderr || r2.stdout);
  assert.match(r1.stdout, new RegExp(expectedSha(files)));
  assert.match(r2.stdout, new RegExp(expectedSha(files)));
});

test("intentional file change yields a different SHA and mismatch exit", () => {
  const base = {
    "a/SKILL.md": "alpha\n",
  };
  const root = makeTree(base);
  const shaPath = join(root, ".agents", "SYNC_SHA.txt");
  writeFileSync(shaPath, expectedSha(base) + "\n");

  const ok = run(root);
  assert.equal(ok.status, 0, ok.stderr || ok.stdout);

  writeFileSync(join(root, ".agents", "skills", "a", "SKILL.md"), "changed\n");
  const bad = run(root);
  assert.equal(bad.status, 1, bad.stderr || bad.stdout);
  assert.notEqual(
    bad.stdout.match(/[a-f0-9]{64}/)?.[0],
    expectedSha(base),
  );
});

test("--write produces SYNC_SHA.txt that then verifies", () => {
  const files = { "x/SKILL.md": "hello\n" };
  const root = makeTree(files);
  const written = run(root, ["--write"]);
  assert.equal(written.status, 0, written.stderr || written.stdout);
  const disk = readFileSync(join(root, ".agents", "SYNC_SHA.txt"), "utf8").trim();
  assert.equal(disk, expectedSha(files));
  const verify = run(root);
  assert.equal(verify.status, 0, verify.stderr || verify.stdout);
});
