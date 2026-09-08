/* One place that decides what a timestamp from the server means.
 *
 * The database holds naive UTC -- "2026-09-12T18:00:00", no Z, no offset --
 * and that is what the API sends. JavaScript reads a date-time in that form
 * as *local*, so every time on screen was shifted by the viewer's offset:
 * an event entered at 13:00 in Austin was stored correctly as 18:00 UTC and
 * then displayed as 18:00. Mail was worse than wrong, it was silently
 * plausible -- anything from the last five hours read as "just now".
 *
 * Two kinds of value arrive, and confusing them is how the obvious fix goes
 * wrong in the other direction:
 *
 *   An *instant* -- when a message arrived, when a meeting starts. This is a
 *   point on the world's timeline and must be read as UTC, then shown in
 *   whatever zone the reader is in.
 *
 *   A *floating date* -- an all-day event. Someone's birthday is the twelfth
 *   everywhere; it is not an instant, and forcing it through a zone moves it
 *   to the eleventh for everyone west of Greenwich. These are stored with a
 *   midnight on the end that means nothing and must be read as local.
 *
 * The server's times.py owns the same decision on the way in. This is its
 * other half, and the two comments should be read together.
 */

// Z, +05:00, -0500 -- anything that already says what it means.
const HAS_ZONE = /([Zz]|[+-]\d{2}:?\d{2})$/;

/**
 * A Date from a server timestamp, or null.
 *
 * `floating` marks an all-day value, which is a calendar date rather than an
 * instant. Pass the event's own all_day flag; do not guess from the string,
 * because an all-day event still arrives with a midnight attached.
 */
export function fromServer(value, { floating = false } = {}) {
  if (!value) return null;
  const s = String(value).trim();
  if (!s) return null;

  let text;
  if (s.length <= 10) {
    // A bare date. Local midnight, so the day is the day it says.
    text = `${s}T00:00:00`;
  } else if (HAS_ZONE.test(s)) {
    text = s;
  } else if (floating) {
    text = s;
  } else {
    text = `${s}Z`;
  }

  const d = new Date(text);
  return Number.isNaN(d.getTime()) ? null : d;
}

export function hhmm(d) {
  const pad = (n) => String(n).padStart(2, "0");
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
