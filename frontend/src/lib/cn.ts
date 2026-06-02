/** Tiny classNames joiner. Filters falsy values; no external dependency. */
export type ClassValue = string | number | false | null | undefined;

export function cn(...values: ClassValue[]): string {
  let out = "";
  for (const v of values) {
    if (!v && v !== 0) continue;
    out += (out ? " " : "") + v;
  }
  return out;
}
