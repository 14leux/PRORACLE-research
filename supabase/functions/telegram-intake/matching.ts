// Matching of read rows to stored fixtures. A row is matched only when exactly one stored, scheduled
// fixture, kicking off within three hours, fits both team names (home with home, away with away) at the
// best level any candidate reaches:
//   EXACT  the names are equal after normalisation (case, accents, apostrophes, punctuation, club affixes, the
//          aa/oe/ae spellings), or are the same words in a different order ("Volos NPS" and "NPS Volos")
//   ALIAS  a name that an earlier matched ticket already tied to the stored name (remembered)
//   LOOSE  one name's words are all contained in the other's ("Brighton" in "Brighton & Hove Albion"),
//          unless the extra words mark a different side (U21, Women, II, B ...) or the shorter name is
//          only generic words ("United", "City")
//   ANCHOR the fallback when no stored fixture fits BOTH names: exactly one fixture in the window fits ONE team
//          (at any level above, home with home or away with away) while the other name fits nothing. A team cannot
//          play twice within three hours, so the other name is taken to be another spelling of that fixture's other
//          team. It is listed back for checking and remembered like a loose match, and it can be switched off
//          with the INTAKE_ANCHOR_MATCHING setting (see anchorEnabled).
// Two or more candidates at the best level is AMBIGUOUS and nothing is used. Loose matches are listed
// back to the operator, and every match is remembered through the stored tickets, so a name is
// resolved the same way next time.

import { SEED_ALIAS_PAIRS } from "./seed_aliases.ts";
import type { ParsedRow } from "./vision.ts";

export interface StoredFixture {
  id: string;
  home_team: string;
  away_team: string;
  kickoff_at: string;
  competition_name: string | null;
}

export type MatchReason = "NO_KICKOFF" | "NO_MATCH" | "AMBIGUOUS";
export type MatchVia = "EXACT" | "ALIAS" | "LOOSE" | "ANCHOR";

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
    // an apostrophe disappears instead of splitting the word: "Be'er Sheva" and "Beer Sheva" are one name
    .replace(/['‘’`´]/g, "")
    .replace(/&/g, " and ")
    // betting sites write ø as oe, å as aa and æ as ae; folding the three to one letter, on both sides, makes
    // "Tromsoe" and "Tromsø", "Vaalerenga" and "Vålerenga" equal. Digits and every other letter are left alone.
    .replace(/aa|oe|ae/g, (pair) => pair[0])
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

/** True when both names have the same words in a different order ("nps volos" and "volos nps"). */
export function sameWords(a: string, b: string): boolean {
  const ta = a.split(" ").filter((t) => t !== "").sort();
  const tb = b.split(" ").filter((t) => t !== "").sort();
  return ta.length > 1 && ta.length === tb.length && ta.every((t, i) => t === tb[i]);
}

/** 0 exact, 1 remembered alias, 2 loose, null no. */
function level(raw: string, stored: string, aliases: AliasMap): 0 | 1 | 2 | null {
  if (raw === stored || sameWords(raw, stored)) return 0;
  if (aliases.get(raw) === stored) return 1;
  return looselySame(raw, stored) ? 2 : null;
}

const VIA: readonly MatchVia[] = ["EXACT", "ALIAS", "LOOSE"];

export interface MatchOptions {
  /** The one-team fallback; on unless a caller says false (production reads INTAKE_ANCHOR_MATCHING). */
  anchor?: boolean;
}

/** The setting that switches the one-team fallback off without a code change: on unless it says off, 0, false or no. */
export function anchorEnabled(raw: string | undefined): boolean {
  return !["off", "0", "false", "no"].includes((raw ?? "").trim().toLowerCase());
}

export function matchRows(
  rows: readonly ParsedRow[],
  fixtures: readonly StoredFixture[],
  aliases: AliasMap = new Map(),
  options: MatchOptions = {},
): MatchedRow[] {
  const anchorOn = options.anchor !== false;
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
    const oneSided: StoredFixture[] = []; // fixtures in the window where exactly one side fits
    for (const p of prepared) {
      if (Math.abs(p.at - at) > MATCH_WINDOW_MS) continue;
      const h = level(home, p.home, aliases);
      const a = level(away, p.away, aliases);
      if (h !== null && a !== null) candidates.push({ f: p.f, level: Math.max(h, a) as 0 | 1 | 2 });
      else if (h !== null || a !== null) oneSided.push(p.f);
    }
    if (candidates.length === 0) {
      // Only when nothing fits both names does one team plus the kickoff time decide; two such fixtures is a guess.
      if (!anchorOn || oneSided.length === 0) return { row, fixtureId: null, reason: "NO_MATCH", via: null };
      if (oneSided.length > 1) return { row, fixtureId: null, reason: "AMBIGUOUS", via: null };
      const hit = oneSided[0];
      return { row, fixtureId: hit.id, reason: null, via: "ANCHOR", matchedTo: `${hit.home_team} v ${hit.away_team}` };
    }
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
/** The built-in seed names (seed_aliases.ts), normalised the way every other name is. */
export function seedAliases(): Map<string, string> {
  const seed = new Map<string, string>();
  for (const [printed, stored] of SEED_ALIAS_PAIRS) {
    const key = normalizeTeam(printed);
    const target = normalizeTeam(stored);
    if (key !== "" && target !== "" && key !== target) seed.set(key, target);
  }
  return seed;
}

export function buildAliases(history: readonly MatchedHistoryRow[], seed: ReadonlyMap<string, string> = seedAliases()): Map<string, string> {
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
  // The seed names come first; the memory then overrides them, and a name the memory tied to two different
  // places is not trusted, so it is removed from the seed too.
  const aliases = new Map<string, string>(seed);
  for (const [raw, targets] of seen) {
    if (targets.size === 1) aliases.set(raw, [...targets][0]);
    else aliases.delete(raw);
  }
  return aliases;
}
