/**
 * Turn "this clock time in the contact's timezone, on this date" into the value a
 * `datetime-local` input needs, which is the viewer's own local time.
 *
 * The browser has no API for "wall time in zone X", so the offset of the zone at that instant is
 * read from `Intl` and applied, twice, so a date that crosses a daylight-saving change still lands
 * on the right hour. An unknown zone falls back to the viewer's own, rather than throwing.
 */

function pad(n: number): string {
  return String(n).padStart(2, "0");
}

export function toLocalInput(date: Date): string {
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function zoneOffsetMinutes(at: Date, timeZone: string): number {
  const parts = Object.fromEntries(
    new Intl.DateTimeFormat("en-US", {
      timeZone, hourCycle: "h23", year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", second: "2-digit",
    }).formatToParts(at).map((p) => [p.type, p.value]),
  );
  const asUtc = Date.UTC(+parts.year, +parts.month - 1, +parts.day, +parts.hour, +parts.minute, +parts.second);
  return Math.round((asUtc - at.getTime()) / 60000);
}

/** `date` is the `YYYY-MM-DD` part of the input's current value; `clock` is `HH:MM` in `timeZone`. */
export function zonedClockToLocalInput(date: string, clock: string, timeZone: string): string {
  const [y, m, d] = date.split("-").map(Number);
  const [hh, mm] = clock.split(":").map(Number);
  const wall = Date.UTC(y, m - 1, d, hh, mm);
  try {
    let instant = wall;
    for (let i = 0; i < 2; i += 1) instant = wall - zoneOffsetMinutes(new Date(instant), timeZone) * 60000;
    return toLocalInput(new Date(instant));
  } catch {
    return `${date}T${clock}`;
  }
}
