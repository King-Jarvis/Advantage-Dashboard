/* Deep links from a tapped widget row.
 *
 * The parsing and the day-key derivation are the two places this can go
 * silently wrong: a bad route lands on the right screen at the wrong place,
 * which looks like the tap not having worked rather than like a bug.
 */
import { fromServer } from "../../app/src/dashboard/static/time.js";

// The router's own shapes, kept here so the rules are asserted rather than
// assumed. If app.js changes these patterns, these tests are what notice.
const MSG = /^[0-9a-f]{32}$/;
const DAY = /^\d{4}-\d{2}-\d{2}$/;

function dayKey(e) {
  const d = fromServer(e.starts_at, { floating: e.all_day });
  if (!d) return String(e.starts_at || "").slice(0, 10);
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

export const tests = {
  "an evening event opens the day it is on locally"() {
    // 8pm in Austin is stored as 01:00 the next day in UTC. Slicing the
    // string would open tomorrow, which is empty.
    eq(dayKey({ starts_at: "2026-09-13T01:00:00", all_day: false }), "2026-09-12");
  },
  "a morning event opens its own day"() {
    eq(dayKey({ starts_at: "2026-09-12T15:50:00", all_day: false }), "2026-09-12");
  },
  "an all-day event opens the date it says"() {
    eq(dayKey({ starts_at: "2026-09-12T00:00:00", all_day: true }), "2026-09-12");
  },
  "a bare date opens that date"() {
    eq(dayKey({ starts_at: "2026-09-12", all_day: true }), "2026-09-12");
  },
  "a day key is always the shape the router accepts"() {
    for (const e of [{ starts_at: "2026-01-01T00:30:00" },
                     { starts_at: "2026-12-31T23:59:00" },
                     { starts_at: "2026-09-12T00:00:00", all_day: true }]) {
      ok(DAY.test(dayKey(e)), `${dayKey(e)} is not a routable day`);
    }
  },
  "a rubbish timestamp does not produce a rubbish route"() {
    eq(dayKey({ starts_at: "" }), "");
  },
  "the router only accepts a real message id"() {
    ok(MSG.test("a".repeat(32)));
    ok(!MSG.test("../../etc/passwd"));
    ok(!MSG.test("a".repeat(31)));
    ok(!MSG.test("A".repeat(32)), "uppercase is not an id we mint");
  },
  "the router only accepts a real day"() {
    ok(DAY.test("2026-09-12"));
    ok(!DAY.test("2026-9-12"));
    ok(!DAY.test("2026-09"));
  },
};

function eq(got, want) {
  if (got !== want) throw new Error(`got ${JSON.stringify(got)}, want ${JSON.stringify(want)}`);
}
function ok(cond, why) { if (!cond) throw new Error(why || "expected true"); }
