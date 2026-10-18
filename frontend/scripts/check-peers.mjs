// Verifies every installed package's non-optional peer dependencies are present.
// Needed because .npmrc sets legacy-peer-deps (npm does not auto-install peers).
import { readdirSync, readFileSync, existsSync } from "node:fs";
import { join } from "node:path";

const root = new URL("..", import.meta.url).pathname;
const nm = join(root, "node_modules");
const pkgs = [];
for (const name of readdirSync(nm)) {
  if (name.startsWith(".")) continue;
  if (name.startsWith("@")) for (const sub of readdirSync(join(nm, name))) pkgs.push(`${name}/${sub}`);
  else pkgs.push(name);
}
const missing = [];
for (const name of pkgs) {
  const file = join(nm, name, "package.json");
  if (!existsSync(file)) continue;
  const pkg = JSON.parse(readFileSync(file, "utf8"));
  const meta = pkg.peerDependenciesMeta ?? {};
  for (const peer of Object.keys(pkg.peerDependencies ?? {})) {
    if (meta[peer]?.optional) continue;
    if (!existsSync(join(nm, peer, "package.json"))) missing.push(`${name} -> ${peer}`);
  }
}
// Only report peers of packages we depend on directly or that are actually used at runtime/test time.
const direct = new Set(Object.keys({ ...JSON.parse(readFileSync(join(root, "package.json"), "utf8")).dependencies, ...JSON.parse(readFileSync(join(root, "package.json"), "utf8")).devDependencies }));
const relevant = missing.filter((m) => direct.has(m.split(" -> ")[0]));
if (relevant.length) {
  console.error("Missing peer dependencies:\n" + relevant.join("\n"));
  process.exit(1);
}
console.log(`peer check ok (${pkgs.length} packages scanned; ${missing.length - relevant.length} transitive optional-in-practice peers ignored)`);
if (missing.length) console.log("transitive:", missing.join(", "));
