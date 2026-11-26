// Matching of read rows to stored fixtures. A row is matched only when exactly one stored, scheduled
// fixture, kicking off within three hours, fits both team names (home with home, away with away) at the
// best level any candidate reaches:
//   EXACT  the names are equal after normalisation (case, accents, punctuation, club affixes)
//   ALIAS  a name that an earlier matched ticket already tied to the stored name (remembered)
//   LOOSE  one name's words are all contained in the other's ("Brighton" in "Brighton & Hove Albion"),
//          unless the extra words mark a different side (U21, Women, II, B ...) or the shorter name is
//          only generic words ("United", "City")
// Two or more candidates at the best level is AMBIGUOUS and nothing is used. Loose matches are listed
// back to the operator, and every match is remembered through the stored tickets, so a name is
// resolved the same way next time.

import type { ParsedRow } from "./vision.ts";

export interface StoredFixture {
  id: string;
  home_team: string;
  away_team: string;
  kickoff_at: string;
  competition_name: string | null;
}

export type MatchReason = "NO_KICKOFF" | "NO_MATCH" | "AMBIGUOUS";
export type MatchVia = "EXACT" | "ALIAS" | "LOOSE";

export interface MatchedRow {
  row: ParsedRow;
  fixtureId: string | null;
  reason: MatchReason | null;
  /** How the stored fixture was found; absent or null when unmatched. */
  via?: MatchVia | null;
  /** The stored fixture's own names, "Home v Away", for the operator to check. */
  matchedTo?: string | null;
}

/** A normalised name the operator's tickets have already tied to a normalised stored name. */
export type AliasMap = ReadonlyMap<string, string>;

/** One stored ticket row that matched a fixture: what the screenshot said, and what is stored. */
export interface MatchedHistoryRow {
  home_team: string;
  away_team: string;
  matched: { home_team: string; away_team: string } | null;
}

const NOISE = new Set(["fc", "cf", "afc", "sc", "ac", "fk", "sk", "bk", "the"]);
// Words that make another team, so "X" must not match "X <word>".
const DIFFERENT_SIDE = new Set(["women", "woman", "ladies", "w", "ii", "iii", "b", "reserves", "reserve", "res", "youth", "academy", "juniors", "junior", "jr", "castilla", "amateure", "primavera"]);
const YOUTH = /^u\d{1,2}$/;
// Names made only of these say too little to identify a club.
const GENERIC = new Set([
  "united", "city", "town", "rovers", "wanderers", "athletic", "albion", "county", "real", "sporting",
  "dynamo", "dinamo", "olympic", "club", "sport", "deportivo", "borussia", "rangers",
]);
export const MATCH_WINDOW_MS = 3 * 60 * 60 * 1000;

export function normalizeTeam(name: string): string {
  return name
    .normalize("NFKD")
    .replace(/[̀-ͯ]/g, "")
    .toLowerCase()
    .replace(/ł/g, "l")
    .replace(/ø/g, "o")
    .replace(/đ/g, "d")
    .replace(/ß/g, "ss")
    .replace(/æ/g, "ae")
    .replace(/œ/g, "oe")
    .replace(/ı/g, "i")
    .replace(/&/g, " and ")
    .replace(/[^a-z0-9]+/g, " ")
    .split(" ")
    .filter((token) => token.length > 0 && !NOISE.has(token))
    .join(" ");
}

/** True when every word of the shorter name is in the longer one and the rest does not change the team. */
export function looselySame(a: string, b: string): boolean {
  const ta = a.split(" ").filter((t) => t !== "");
  const tb = b.split(" ").filter((t) => t !== "");
  const [short, long] = ta.length <= tb.length ? [ta, tb] : [tb, ta];
  if (short.length === 0 || short.length === long.length) return false;
  if (!short.every((t) => long.includes(t))) return false;
  if (!short.some((t) => t.length >= 4)) return false;
  if (short.every((t) => GENERIC.has(t))) return false;
  return !long.filter((t) => !short.includes(t)).some((t) => DIFFERENT_SIDE.has(t) || YOUTH.test(t));
}

/** 0 exact, 1 remembered alias, 2 loose, null no. */
function level(raw: string, stored: string, aliases: AliasMap): 0 | 1 | 2 | null {
  if (raw === stored) return 0;
  if (aliases.get(raw) === stored) return 1;
  return looselySame(raw, stored) ? 2 : null;
}

const VIA: readonly MatchVia[] = ["EXACT", "ALIAS", "LOOSE"];

export function matchRows(
  rows: readonly ParsedRow[],
  fixtures: readonly StoredFixture[],
  aliases: AliasMap = new Map(),
): MatchedRow[] {
  const prepared = fixtures.map((f) => ({
    f,
    home: normalizeTeam(f.home_team),
    away: normalizeTeam(f.away_team),
    at: Date.parse(f.kickoff_at),
  }));
  return rows.map((row): MatchedRow => {
    if (row.kickoff_at === null) return { row, fixtureId: null, reason: "NO_KICKOFF", via: null };
    const at = Date.parse(row.kickoff_at);
    const home = normalizeTeam(row.home_team);
    const away = normalizeTeam(row.away_team);
    if (home === "" || away === "") return { row, fixtureId: null, reason: "NO_MATCH", via: null };
    const candidates: { f: StoredFixture; level: 0 | 1 | 2 }[] = [];
    for (const p of prepared) {
      if (Math.abs(p.at - at) > MATCH_WINDOW_MS) continue;
      const h = level(home, p.home, aliases);
      const a = level(away, p.away, aliases);
      if (h !== null && a !== null) candidates.push({ f: p.f, level: Math.max(h, a) as 0 | 1 | 2 });
    }
    if (candidates.length === 0) return { row, fixtureId: null, reason: "NO_MATCH", via: null };
    const best = Math.min(...candidates.map((c) => c.level));
    const group = candidates.filter((c) => c.level === best);
    if (group.length !== 1) return { row, fixtureId: null, reason: "AMBIGUOUS", via: null };
    const hit = group[0].f;
    return { row, fixtureId: hit.id, reason: null, via: VIA[best], matchedTo: `${hit.home_team} v ${hit.away_team}` };
  });
}

/**
 * The names earlier tickets have already tied to stored names. A name that was tied to two different
 * stored names is dropped (it is not safe to remember), and a name equal to its stored name needs none.
 */
export function buildAliases(history: readonly MatchedHistoryRow[]): Map<string, string> {
  const seen = new Map<string, Set<string>>();
  const add = (raw: string, stored: string) => {
    const r = normalizeTeam(raw);
    const s = normalizeTeam(stored);
    if (r === "" || s === "" || r === s) return;
    const targets = seen.get(r) ?? new Set<string>();
    targets.add(s);
    seen.set(r, targets);
  };
  for (const h of history) {
    if (!h.matched) continue;
    add(h.home_team, h.matched.home_team);
    add(h.away_team, h.matched.away_team);
  }
  const aliases = new Map<string, string>();
  for (const [raw, targets] of seen) if (targets.size === 1) aliases.set(raw, [...targets][0]);
  return aliases;
}
