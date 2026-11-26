// Strict matching of read rows to stored fixtures. A row is matched only when exactly one stored,
// scheduled fixture has both team names equal after normalisation and kicks off within three hours.
// Anything else is reported as unmatched and never used: no fuzzy guessing.

import type { ParsedRow } from "./vision.ts";

export interface StoredFixture {
  id: string;
  home_team: string;
  away_team: string;
  kickoff_at: string;
  competition_name: string | null;
}

export type MatchReason = "NO_KICKOFF" | "NO_MATCH" | "AMBIGUOUS";

export interface MatchedRow {
  row: ParsedRow;
  fixtureId: string | null;
  reason: MatchReason | null;
}

const NOISE = new Set(["fc", "cf", "afc", "sc", "ac", "fk", "sk", "bk", "the"]);
export const MATCH_WINDOW_MS = 3 * 60 * 60 * 1000;

export function normalizeTeam(name: string): string {
  return name
    .normalize("NFKD")
    .replace(/[̀-ͯ]/g, "")
    .toLowerCase()
    .replace(/&/g, " and ")
    .replace(/[^a-z0-9]+/g, " ")
    .split(" ")
    .filter((token) => token.length > 0 && !NOISE.has(token))
    .join(" ");
}

export function matchRows(rows: readonly ParsedRow[], fixtures: readonly StoredFixture[]): MatchedRow[] {
  const prepared = fixtures.map((f) => ({
    id: f.id,
    home: normalizeTeam(f.home_team),
    away: normalizeTeam(f.away_team),
    at: Date.parse(f.kickoff_at),
  }));
  return rows.map((row): MatchedRow => {
    if (row.kickoff_at === null) return { row, fixtureId: null, reason: "NO_KICKOFF" };
    const at = Date.parse(row.kickoff_at);
    const home = normalizeTeam(row.home_team);
    const away = normalizeTeam(row.away_team);
    if (home === "" || away === "") return { row, fixtureId: null, reason: "NO_MATCH" };
    const hits = prepared.filter((f) => f.home === home && f.away === away && Math.abs(f.at - at) <= MATCH_WINDOW_MS);
    if (hits.length === 1) return { row, fixtureId: hits[0].id, reason: null };
    return { row, fixtureId: null, reason: hits.length === 0 ? "NO_MATCH" : "AMBIGUOUS" };
  });
}
