// The plain-language performance message (/eli5). Built only from counts the database already holds, with
// fixed templates and no model call. It reports two windows (the last 7 days and the last 28 days), compares
// them, and calls out ticket results that stood out either way. It calls an arm "working" or "dropping" only
// when there are enough settled picks and the gap from arm A is bigger than the margin of error the scoreboard
// already shows; in every other case it says it is too early to tell. It never mentions stakes, profit or
// what to back.

import type { ScoreRow, SlateScoreRow, TicketHitRow, WindowRow } from "./store.ts";

/** An arm needs at least this many settled picks in a window before it is called working or dropping. */
export const MIN_SETTLED_FOR_VERDICT = 20;

/** The rough 95% margin of error for one hit rate, in points (1.96 * sqrt(0.25 / n)); the scoreboard uses the same. */
export const marginPoints = (n: number): number => Math.round(98 / Math.sqrt(n));

export type Verdict =
  | { kind: "working"; gap: number; margin: number }
  | { kind: "dropping"; gap: number; margin: number }
  | { kind: "too_early"; reason: "few_picks" | "inside_margin"; gap: number | null; margin: number | null };

/** One non-A arm over one window against arm A on exactly the same fixtures. */
export function judge(window: Pick<WindowRow, "picks_settled" | "picks_right" | "a_right_same_fixtures"> | undefined): Verdict {
  if (window === undefined || window.picks_settled < MIN_SETTLED_FOR_VERDICT) {
    return { kind: "too_early", reason: "few_picks", gap: null, margin: null };
  }
  const n = window.picks_settled;
  const gap = (100 * (window.picks_right - window.a_right_same_fixtures)) / n;
  const margin = marginPoints(n);
  if (gap > margin) return { kind: "working", gap, margin };
  if (gap < -margin) return { kind: "dropping", gap, margin };
  return { kind: "too_early", reason: "inside_margin", gap, margin };
}

// --- ticket spikes ----------------------------------------------------------------------------------

/** A ticket arm needs at least this many settled picks on that ticket to be checked. */
export const SPIKE_MIN_TICKET_PICKS = 8;
/** Its baseline is the same arm's other tickets of that kind: at least this many tickets ... */
export const SPIKE_MIN_BASELINE_TICKETS = 3;
/** ... and at least this many settled picks between them. */
export const SPIKE_MIN_BASELINE_PICKS = 40;
/** A spike is a ticket rate more than this many standard errors from the baseline rate, either way. */
export const SPIKE_STANDARD_ERRORS = 2;
export const SPIKE_MAX_SHOWN = 5;
/** Only tickets with a settled kickoff in this many days are checked or used as a baseline. */
export const SPIKE_DAYS = 28;

export interface Spike {
  ticketId: string;
  kind: string;
  ticketDate: string;
  arm: string;
  version: string;
  right: number;
  settled: number;
  baselineRate: number;
  baselineTickets: number;
  baselinePicks: number;
  direction: "high" | "low";
  standardErrors: number;
  lastKickoffMs: number;
}

export interface SpikeCheck {
  spikes: Spike[];
  /** Ticket arms that had enough picks and a usable baseline, so a spike could have been called. */
  checked: number;
}

/**
 * Ticket arms whose hit rate is far from the same arm's rate on the other tickets of that kind over the last
 * 28 days (the ticket is left out of its own baseline). Pure counts in, counts out. About 1 in 20 ordinary
 * tickets will land outside two standard errors by chance, which the message says.
 */
export function findSpikes(hits: readonly TicketHitRow[], now: Date): SpikeCheck {
  const cutoff = now.getTime() - SPIKE_DAYS * 24 * 3600 * 1000;
  const recent = hits.filter((h) => Date.parse(h.last_kickoff_at) >= cutoff);
  const spikes: Spike[] = [];
  let checked = 0;
  for (const h of recent) {
    if (h.picks_settled < SPIKE_MIN_TICKET_PICKS) continue;
    // arm_version is unique to an arm (A and C have one fixed version each, a rule is rule-vN), so it also tells the arms apart
    const others = recent.filter((o) =>
      o.ticket_id !== h.ticket_id && o.ticket_kind === h.ticket_kind && o.arm_version === h.arm_version
    );
    const picks = others.reduce((n, o) => n + o.picks_settled, 0);
    if (others.length < SPIKE_MIN_BASELINE_TICKETS || picks < SPIKE_MIN_BASELINE_PICKS) continue;
    const right = others.reduce((n, o) => n + o.picks_right, 0);
    const p = right / picks;
    if (p <= 0 || p >= 1) continue; // a baseline of all or none right has no spread to measure against
    const standardError = Math.sqrt((p * (1 - p)) / h.picks_settled);
    const rate = h.picks_right / h.picks_settled;
    const z = (rate - p) / standardError;
    checked++;
    if (Math.abs(z) > SPIKE_STANDARD_ERRORS) {
      spikes.push({
        ticketId: h.ticket_id,
        kind: h.ticket_kind,
        ticketDate: h.ticket_date,
        arm: h.arm,
        version: h.arm_version,
        right: h.picks_right,
        settled: h.picks_settled,
        baselineRate: p,
        baselineTickets: others.length,
        baselinePicks: picks,
        direction: z > 0 ? "high" : "low",
        standardErrors: Math.abs(z),
        lastKickoffMs: Date.parse(h.last_kickoff_at),
      });
    }
  }
  spikes.sort((a, b) =>
    b.lastKickoffMs - a.lastKickoffMs || order({ kind: a.kind, arm: a.arm, version: a.version }, { kind: b.kind, arm: b.arm, version: b.version })
  );
  return { spikes, checked };
}

// --- wording ----------------------------------------------------------------------------------------

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

/** A, then B2, B10 ..., then C (the same order as the scoreboard); ties by ticket kind. */
function order(a: ArmKey, b: ArmKey): number {
  const x = a.arm, y = b.arm;
  if (x !== y) return x < y ? -1 : 1;
  const n = (Number(short(a.arm, a.version).slice(1)) || 0) - (Number(short(b.arm, b.version).slice(1)) || 0);
  return n !== 0 ? n : a.kind < b.kind ? -1 : a.kind > b.kind ? 1 : 0;
}

const KIND_TITLE: Record<string, string> = { SIMULATED: "Simulated tickets", SCREENSHOT: "Screenshot tickets" };
const KIND_ONE: Record<string, string> = { SIMULATED: "Simulated ticket", SCREENSHOT: "Screenshot ticket" };

const windowRow = (rows: readonly WindowRow[], key: ArmKey, name: WindowRow["window_name"]): WindowRow | undefined =>
  rows.find((r) => keyOf(r) === `${key.kind}|${key.arm}|${key.version}` && r.window_name === name);

const tally = (w: WindowRow | undefined): string =>
  w === undefined || w.picks_settled === 0 ? "nothing settled" : `right ${w.picks_right} of ${w.picks_settled} (${pct(w.picks_right, w.picks_settled)})`;

function verdictText(verdict: Verdict, w: WindowRow | undefined): string {
  if (verdict.kind === "working") {
    return `Working: it is ahead of A by ${points(verdict.gap)} on the same ${w!.picks_settled} matches, more than the ±${verdict.margin} points of noise I allow for.`;
  }
  if (verdict.kind === "dropping") {
    return `Dropping: it is behind A by ${points(verdict.gap)} on the same ${w!.picks_settled} matches, more than the ±${verdict.margin} points of noise I allow for.`;
  }
  if (verdict.reason === "few_picks") {
    const have = w === undefined ? 0 : w.picks_settled;
    return `Too early to tell: only ${have} settled ${plural(have, "pick", "picks")}, and I wait for ${MIN_SETTLED_FOR_VERDICT}.`;
  }
  return `Too early to tell: its gap from A (${verdict.gap! >= 0 ? "+" : "-"}${points(verdict.gap!)}) is inside the ±${verdict.margin} points of noise.`;
}

const phrase = (v: Verdict): string => (v.kind === "working" ? "working" : v.kind === "dropping" ? "dropping" : "too early to tell");

/** One sentence comparing an arm's verdicts over the two windows. */
export function compareWindows(v7: Verdict, w7: WindowRow | undefined, v28: Verdict, w28: WindowRow | undefined): string {
  if (v7.kind === v28.kind) {
    return v7.kind === "too_early" ? "Both windows say too early to tell." : `Same story over 7 and 28 days: ${phrase(v7)}.`;
  }
  let text = `This week differs from the month: ${phrase(v7)} over 7 days, ${phrase(v28)} over 28 days.`;
  if (v7.kind === "too_early" && v7.reason === "few_picks") {
    const n = w7 === undefined ? 0 : w7.picks_settled;
    text += ` The 7-day window has only ${n} settled ${plural(n, "pick", "picks")}.`;
  } else if (v28.kind === "too_early" && v28.reason === "few_picks") {
    const n = w28 === undefined ? 0 : w28.picks_settled;
    text += ` The 28-day window has only ${n} settled ${plural(n, "pick", "picks")}.`;
  }
  return text;
}

function armLines(key: ArmKey, rows: readonly WindowRow[], all: ScoreRow | undefined): string[] {
  const w7 = windowRow(rows, key, "LAST7");
  const w28 = windowRow(rows, key, "LAST28");
  const prior = windowRow(rows, key, "PRIOR28");
  const lines = [`- ${plainName(key.arm, key.version)}: last 7 days ${tally(w7)}; last 28 days ${tally(w28)}.`];
  if (key.arm !== "A") {
    const v7 = judge(w7);
    const v28 = judge(w28);
    lines.push(`  Last 7 days: ${verdictText(v7, w7)}`);
    lines.push(`  Last 28 days: ${verdictText(v28, w28)}`);
    lines.push(`  ${compareWindows(v7, w7, v28, w28)}`);
  }
  if (prior !== undefined && prior.picks_settled > 0) {
    lines.push(`  The 28 days before: right ${prior.picks_right} of ${prior.picks_settled} (${pct(prior.picks_right, prior.picks_settled)}).`);
  }
  if (all !== undefined) {
    lines.push(`  Since the start: right ${all.picks_right} of ${all.picks_settled} (${pct(all.picks_right, all.picks_settled)}).`);
  }
  return lines;
}

/** The ticket check, as lines. Null means the ticket view could not be read, so the section is left out. */
export function spikeLines(check: SpikeCheck | null): string[] {
  if (check === null) return [];
  if (check.spikes.length > 0) {
    const lines = [
      `Tickets that stood out (a ticket's hit rate against the same arm's other tickets of that kind over the last ${SPIKE_DAYS} days):`,
    ];
    for (const s of check.spikes.slice(0, SPIKE_MAX_SHOWN)) {
      lines.push(
        `- ${KIND_ONE[s.kind] ?? s.kind} ${s.ticketDate}, ${short(s.arm, s.version)}: right ${s.right} of ${s.settled} (${pct(s.right, s.settled)}), ` +
          `unusually ${s.direction} against its usual ${Math.round(100 * s.baselineRate)}% over ${s.baselineTickets} other ${plural(s.baselineTickets, "ticket", "tickets")} (${s.baselinePicks} picks).`,
      );
    }
    if (check.spikes.length > SPIKE_MAX_SHOWN) lines.push(`- and ${check.spikes.length - SPIKE_MAX_SHOWN} more.`);
    lines.push("About 1 ticket in 20 looks like this by chance alone, so read one as a prompt to look, not as a finding.");
    return lines;
  }
  if (check.checked === 0) {
    return [
      `Ticket check: not enough tickets yet to call a spike. I need a ticket arm with ${SPIKE_MIN_TICKET_PICKS} settled picks and ${SPIKE_MIN_BASELINE_TICKETS} other tickets of the same kind ` +
        `with ${SPIKE_MIN_BASELINE_PICKS} settled picks between them, all in the last ${SPIKE_DAYS} days.`,
    ];
  }
  return [`Ticket check: no ticket stood out against its arm's usual rate (${check.checked} ${plural(check.checked, "ticket result", "ticket results")} checked).`];
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
  "Here is how the picks are doing, in plain words. It comes from counts of matches that are over and have a stored result, over the last 7 days and the last 28 days. " +
  `I only call an arm working or dropping once it has ${MIN_SETTLED_FOR_VERDICT} settled picks in that window and is ahead of or behind arm A by more than the margin of error. Until then: too early to tell.`;

export const ELI5_CLOSING = "This is a calibration report card. It is not betting advice.";

/**
 * The whole message, as one string (the handler splits it if it is long). Pure: the same counts and clock give
 * the same words. `hits` is null when the ticket view could not be read; the ticket section is then left out.
 */
export function buildEli5(
  score: readonly ScoreRow[],
  windows: readonly WindowRow[],
  slate: SlateScoreRow | null,
  hits: readonly TicketHitRow[] | null,
  now: Date,
): string {
  const keys = new Map<string, ArmKey>();
  const note = (r: { ticket_kind: string; arm: string; arm_version: string }) =>
    keys.set(keyOf(r), { kind: r.ticket_kind, arm: r.arm, version: r.arm_version });
  for (const r of score) note(r);
  for (const r of windows) note(r);

  const lines: string[] = [ELI5_INTRO];
  const kinds = [...new Set([...keys.values()].map((k) => k.kind))].sort();
  if (kinds.length === 0) {
    lines.push("", "Nothing has settled yet, so it is too early to tell about anything.");
  }
  for (const kind of kinds) {
    lines.push("", `${KIND_TITLE[kind] ?? kind}:`);
    const arms = [...keys.values()].filter((k) => k.kind === kind).sort(order);
    for (const key of arms) {
      const all = score.find((r) => keyOf(r) === `${key.kind}|${key.arm}|${key.version}`);
      lines.push(...armLines(key, windows, all));
    }
  }
  if (kinds.length > 0) {
    const ticketLines = spikeLines(hits === null ? null : findSpikes(hits, now));
    if (ticketLines.length > 0) lines.push("", ...ticketLines);
  }
  const slates = slateLine(slate);
  if (slates !== null) lines.push("", slates);
  lines.push("", ELI5_CLOSING);
  return lines.join("\n");
}
