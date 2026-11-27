// Settings read from the function's environment. Every one has a safe default: a missing, empty,
// non-numeric or out-of-range value falls back to the default, so a bad setting can never make the
// bot refuse everything (a budget of 0, for instance, is ignored rather than obeyed).

export function intSetting(raw: string | undefined, fallback: number, min: number, max: number): number {
  if (raw === undefined || raw.trim() === "") return fallback;
  const n = Number(raw);
  return Number.isInteger(n) && n >= min && n <= max ? n : fallback;
}
