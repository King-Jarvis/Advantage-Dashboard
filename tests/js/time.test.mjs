/* What a server timestamp means, tested in a zone that is not UTC.
 *
 * Every one of these passed trivially before the bug was found, because the
 * suite ran in UTC where the wrong answer and the right one are identical.
 * The runner pins TZ to America/Chicago for exactly that reason: a timezone
 * bug is invisible in the timezone it was written in.
 */
import { fromServer, hhmm } from "../../app/src/dashboard/static/time.js";

export const tests = {
  "an evening event keeps its own clock time"() {
    // Entered as 1pm in Austin, stored as 18:00 UTC.
    eq(hhmm(fromServer("2026-09-12T18:00:00")), "13:00");
  },
  "a time just before midnight stays on its own day"() {
    const d = fromServer("2026-09-13T04:59:00");
    eq(hhmm(d), "23:59");
    eq(d.getDate(), 12);
  },
  "a synced morning event is not shifted into the afternoon"() {
    eq(hhmm(fromServer("2027-09-05T15:50:00")), "10:50");
  },
  "an all-day event does not slide to the day before"() {
    eq(fromServer("2027-08-24T00:00:00", { floating: true }).getDate(), 24);
  },
  "a bare date is the day it says"() {
    eq(fromServer("2027-08-24").getDate(), 24);
  },
  "a value that already states its zone is left alone"() {
    eq(hhmm(fromServer("2026-09-12T18:00:00Z")), "13:00");
    eq(hhmm(fromServer("2026-09-12T13:00:00-05:00")), "13:00");
    eq(hhmm(fromServer("2026-09-12T13:00:00-0500")), "13:00");
  },
  "a message from an hour ago is not in the future"() {
    // The failure that read as "just now" for everything recent.
    const hourAgo = new Date(Date.now() - 3600e3)
      .toISOString().slice(0, 19);
    const parsed = fromServer(hourAgo);
    ok(parsed <= new Date(), "parsed timestamp must not be in the future");
  },
  "absence is null, never a wrong date"() {
    for (const v of ["", null, undefined, "not a date"]) eq(fromServer(v), null);
  },
};

function eq(got, want) {
  if (got !== want && !(got === null && want === null)) {
    throw new Error(`got ${JSON.stringify(got)}, want ${JSON.stringify(want)}`);
  }
}
function ok(cond, why) { if (!cond) throw new Error(why); }
