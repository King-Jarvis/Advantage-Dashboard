/* Runs every *.test.mjs beside it. No framework: a test file exports a
 * `tests` object of name -> function, and a throw is a failure. */
import { readdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
let passed = 0;
const failures = [];

for (const file of readdirSync(here).filter((f) => f.endsWith(".test.mjs"))) {
  const mod = await import(pathToFileURL(join(here, file)).href);
  for (const [name, fn] of Object.entries(mod.tests || {})) {
    try {
      await fn();
      passed++;
    } catch (e) {
      failures.push(`${file} :: ${name}\n      ${e.message}`);
    }
  }
}

for (const f of failures) console.error("  FAILED  " + f);
console.log(failures.length
  ? `\n${failures.length} failed, ${passed} passed`
  : `clean — ${passed} front-end behaviour tests pass (TZ=${
      Intl.DateTimeFormat().resolvedOptions().timeZone})`);
process.exit(failures.length ? 1 : 0);
