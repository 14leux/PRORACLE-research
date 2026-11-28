// The plain-language performance message (/eli5). Built only from counts the database already holds, with
// fixed templates and no model call. It calls an arm "working" or "dropping" only when there are enough
// settled picks and the gap from arm A is bigger than the margin of error the scoreboard already shows;
// in every other case it says it is too early to tell. It never mentions stakes, profit or what to back.

import type { RecentRow, ScoreRow, SlateScoreRow } from "./store.ts";

/** An arm needs at least this many settled picks in the last 28 days before it is called working or dropping. */
export const MIN_SETTLED_FOR_VERDICT = 20;

/** The rough 95% margin of error for one hit rate, in points (1.96 * sqrt(0.25 / n)); the scoreboard uses the same. */
export const marginPoints = (n: number): number => Math.round(98 / Math.sqrt(n));

export type Verdict =
  | { kind: "working"; gap: number; margin: number }
  | { kind: "dropping"; gap: number; margin: number }
  | { kind: "too_early"; reason: "few_picks" | "inside_margin"; gap: number | null; margin: number | null };

/** One non-A arm over the last 28 days against arm A on exactly the same fixtures. */
export function judge(recent: Pick<RecentRow, "picks_settled" | "picks_right" | "a_right_same_fixtures"> | undefined): Verdict {
  if (recent === undefined || recent.picks_settled < MIN_SETTLED_FOR_VERDICT) {
    return { kind: "too_early", reason: "few_picks", gap: null, margin: null };
  }
  const n = recent.picks_settled;
  const gap = (100 * (recent.picks_right - recent.a_right_same_fixtures)) / n;
  const margin = marginPoints(n);
  if (gap > margin) return { kind: "working", gap, margin };
  if (gap < -margin) return { kind: "dropping", gap, margin };
  return { kind: "too_early", reason: "inside_margin", gap, margin };
}

const pct = (right: number, total: number): string => (total === 0 ? "n/a" : `${Math.round((100 * right) / total)}%`);
const points = (value: number): string => `${Math.abs(value).toFixed(1)} points`;
const plural = (n: number, one: string, many: string): string => (n === 1 ? one : many);

interface ArmKey {
  kind: string;
  arm: string;
  version: string;
}
const keyOf = (r: { ticket_kind: string; arm: string; arm_version: string }): string => `${r.ticket_kind}|${r.arm}|${r.arm_version}`;

const short = (arm: string, version: string): string => (arm === "B" ? `B${version.replace(/^rule-v/, "")}` : arm);

function plainName(arm: string, version: string): string {
  if (arm === "A") return "A, the rating model";
  if (arm === "C") return "C, the draw-heavy picker";
  return `${short(arm, version)}, your rule v${version.replace(/^rule-v/, "")}`;
}

/** A, then B2, B10 ..., then C (the same order as the scoreboard). */
function order(a: ArmKey, b: ArmKey): number {
  const x = a.arm, y = b.arm;
  if (x !== y) return x < y ? -1 : 1;
  return (Number(short(a.arm, a.version).slice(1)) || 0) - (Number(short(b.arm, b.version).slice(1)) || 0);
}

const KIND_TITLE: Record<string, string> = { SIMULATED: "Simulated tickets", SCREENSHOT: "Screenshot tickets" };

function armLines(key: ArmKey, recent: RecentRow | undefined, prior: RecentRow | undefined, all: ScoreRow | undefined): string[] {
  const name = plainName(key.arm, key.version);
  const lines: string[] = [];
  if (recent === undefined) {
    lines.push(`- ${name}: nothing settled in the last 28 days.`);
  } else {
    lines.push(`- ${name}: right ${recent.picks_right} of ${recent.picks_settled} in the last 28 days (${pct(recent.picks_right, recent.picks_settled)}).`);
  }
  if (key.arm !== "A") {
    const verdict = judge(recent);
    if (verdict.kind === "working") {
      lines.push(
        `  Working: it is ahead of A by ${points(verdict.gap)} on the same ${recent!.picks_settled} matches, more than the ±${verdict.margin} points of noise I allow for.`,
      );
    } else if (verdict.kind === "dropping") {
      lines.push(
        `  Dropping: it is behind A by ${points(verdict.gap)} on the same ${recent!.picks_settled} matches, more than the ±${verdict.margin} points of noise I allow for.`,
      );
    } else if (verdict.reason === "few_picks") {
      const have = recent === undefined ? 0 : recent.picks_settled;
      lines.push(`  Too early to tell: only ${have} settled ${plural(have, "pick", "picks")}, and I wait for ${MIN_SETTLED_FOR_VERDICT}.`);
    } else {
      lines.push(
        `  Too early to tell: its gap from A (${verdict.gap! >= 0 ? "+" : "-"}${points(verdict.gap!)}) is inside the ±${verdict.margin} points of noise.`,
      );
    }
  }
  if (prior !== undefined && prior.picks_settled > 0) {
    lines.push(`  The 28 days before: right ${prior.picks_right} of ${prior.picks_settled} (${pct(prior.picks_right, prior.picks_settled)}).`);
  }
  if (all !== undefined) {
    lines.push(`  Since the start: right ${all.picks_right} of ${all.picks_settled} (${pct(all.picks_right, all.picks_settled)}).`);
  }
  return lines;
}

/** One line about settled slates, or null while none has settled. Counts only. */
export function slateLine(slate: SlateScoreRow | null): string | null {
  if (slate === null || slate.settled <= 0) return null;
  let line = `Slates: for the ${slate.settled} settled ${plural(slate.settled, "match", "matches")} on them (each counted once), the rating model's pick was right ${slate.model_hits} times (${pct(slate.model_hits, slate.settled)}).`;
  if (slate.compared > 0) {
    line += ` Where all three prices were printed (${slate.compared}), the market's favourite was right ${slate.compared_market_hits} times and the rating model ${slate.compared_model_hits}.`;
  }
  return line;
}

export const ELI5_INTRO =
  "Here is how the picks are doing, in plain words. It comes from counts of matches that are over and have a stored result. " +
  `I only call an arm working or dropping once it has ${MIN_SETTLED_FOR_VERDICT} settled picks in the last 28 days and is ahead of or behind arm A by more than the margin of error. Until then: too early to tell.`;

export const ELI5_CLOSING = "This is a calibration report card. It is not betting advice.";

/** The whole message, as one string (the handler splits it if it is long). Pure: the same counts give the same words. */
export function buildEli5(score: readonly ScoreRow[], recent: readonly RecentRow[], slate: SlateScoreRow | null): string {
  const keys = new Map<string, ArmKey>();
  const note = (r: { ticket_kind: string; arm: string; arm_version: string }) =>
    keys.set(keyOf(r), { kind: r.ticket_kind, arm: r.arm, version: r.arm_version });
  for (const r of score) note(r);
  for (const r of recent) note(r);

  const lines: string[] = [ELI5_INTRO];
  const kinds = [...new Set([...keys.values()].map((k) => k.kind))].sort();
  if (kinds.length === 0) {
    lines.push("", "Nothing has settled yet, so it is too early to tell about anything.");
  }
  for (const kind of kinds) {
    lines.push("", `${KIND_TITLE[kind] ?? kind}:`);
    const arms = [...keys.values()].filter((k) => k.kind === kind).sort(order);
    for (const key of arms) {
      const find = (window: "RECENT" | "PRIOR") =>
        recent.find((r) => keyOf(r) === `${key.kind}|${key.arm}|${key.version}` && r.window_name === window);
      const all = score.find((r) => keyOf(r) === `${key.kind}|${key.arm}|${key.version}`);
      lines.push(...armLines(key, find("RECENT"), find("PRIOR"), all));
    }
  }
  const slates = slateLine(slate);
  if (slates !== null) lines.push("", slates);
  lines.push("", ELI5_CLOSING);
  return lines.join("\n");
}
