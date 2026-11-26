// Rule messages become a fixed structure by a small deterministic grammar, never by a model: free
// text cannot smuggle anything into a rule. The structure is echoed back and activated only by YES.

import { MAX_RULE_TEXT_CHARS } from "./commands.ts";

export type ReverseBasis = "margin" | "odds_disagreement";

export interface RuleV1 {
  v: 1;
  force_draws?: { count: number; basis: "draw_score" };
  reverse?: { count: number; basis: ReverseBasis };
  only_leagues?: string[];
  skip_leagues?: string[];
}

export type ParsedRule = { ok: true; rule: RuleV1 } | { ok: false; error: string };

const MAX_COUNT = 10;
const MAX_LEAGUES = 10;
const MAX_LEAGUE_CHARS = 60;
const LEAGUE_NAME = /^[\p{L}\p{N}][\p{L}\p{N} .'&\-/]*$/u;

function count(raw: string): number | null {
  const n = Number(raw);
  return Number.isInteger(n) && n >= 1 && n <= MAX_COUNT ? n : null;
}

function leagues(raw: string): string[] | string {
  const names = raw.split(",").map((s) => s.trim().replace(/\s+/g, " ")).filter((s) => s.length > 0);
  if (names.length === 0) return "name at least one league";
  if (names.length > MAX_LEAGUES) return `at most ${MAX_LEAGUES} leagues`;
  for (const n of names) {
    if (n.length > MAX_LEAGUE_CHARS || !LEAGUE_NAME.test(n)) return `league name not allowed: ${n.slice(0, 30)}`;
  }
  return [...new Set(names)];
}

export function parseRule(text: string): ParsedRule {
  const input = text.trim();
  if (input.length === 0) return { ok: false, error: "Say what the rule should do, for example: /rule 5 draws; 4 upsets by margin" };
  if (input.length > MAX_RULE_TEXT_CHARS) return { ok: false, error: `Keep a rule under ${MAX_RULE_TEXT_CHARS} characters.` };
  const rule: RuleV1 = { v: 1 };
  for (const raw of input.split(";")) {
    const part = raw.trim().replace(/\s+/g, " ");
    if (part.length === 0) continue;
    const lower = part.toLowerCase();
    let m: RegExpMatchArray | null;
    if ((m = lower.match(/^(?:force )?(\d{1,2}) draws?(?: by draw ?score)?$/))) {
      const n = count(m[1]);
      if (n === null || rule.force_draws) return { ok: false, error: `Unclear directive: "${part.slice(0, 40)}". Use 1 to ${MAX_COUNT} once.` };
      rule.force_draws = { count: n, basis: "draw_score" };
    } else if (
      (m = lower.match(/^(\d{1,2}) (?:upsets?|reversals?|reversed)(?: by (margin|odds(?: disagreement)?))?$/)) ||
      (m = lower.match(/^reverse (\d{1,2})(?: by (margin|odds(?: disagreement)?))?$/))
    ) {
      const n = count(m[1]);
      if (n === null || rule.reverse) return { ok: false, error: `Unclear directive: "${part.slice(0, 40)}". Use 1 to ${MAX_COUNT} once.` };
      rule.reverse = { count: n, basis: m[2] && m[2].startsWith("odds") ? "odds_disagreement" : "margin" };
    // "leagues:" is a keyword only with its colon, so a league called "League 1" keeps its name.
    } else if ((m = part.match(/^only (?:leagues? ?: ?)?(.+)$/i)) || (m = part.match(/^skip (?:leagues? ?: ?)?(.+)$/i))) {
      const names = leagues(m[1]);
      if (typeof names === "string") return { ok: false, error: `League list problem: ${names}.` };
      const key = lower.startsWith("only") ? "only_leagues" : "skip_leagues";
      if (rule[key]) return { ok: false, error: `Give "${key === "only_leagues" ? "only" : "skip"}" once.` };
      rule[key] = names;
    } else {
      return {
        ok: false,
        error:
          `I do not understand "${part.slice(0, 40)}". Directives: N draws, N upsets by margin|odds, only <leagues>, skip <leagues>.`,
      };
    }
  }
  if (!rule.force_draws && !rule.reverse && !rule.only_leagues && !rule.skip_leagues) {
    return { ok: false, error: "No directive found. Example: /rule 5 draws; 4 upsets by margin" };
  }
  if (rule.only_leagues && rule.skip_leagues) {
    return { ok: false, error: "Use either \"only\" or \"skip\", not both." };
  }
  return { ok: true, rule };
}

export function describeRule(rule: RuleV1): string {
  const parts: string[] = [];
  if (rule.force_draws) parts.push(`force the ${rule.force_draws.count} highest draw scores to DRAW`);
  if (rule.reverse) {
    const basis = rule.reverse.basis === "margin" ? "smallest model margin" : "largest model-vs-market gap (needs odds)";
    parts.push(`reverse ${rule.reverse.count} picks chosen by ${basis}`);
  }
  if (rule.only_leagues) parts.push(`only leagues: ${rule.only_leagues.join(", ")}`);
  if (rule.skip_leagues) parts.push(`skip leagues: ${rule.skip_leagues.join(", ")}`);
  return parts.join("; ");
}
