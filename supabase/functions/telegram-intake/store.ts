// Database access for the intake: an interface (so tests use an in-memory fake) and the PostgREST
// implementation. It touches only the proracle intake and ticket tables, as service_role, and never
// stores an image or message text.

import type { RuleV1 } from "./rules.ts";
import type { MatchedRow, StoredFixture } from "./matching.ts";
import type { FetchLike } from "./vision.ts";

export type UpdateKind = "IMAGE" | "PDF" | "COMMAND" | "TEXT" | "REJECTED";
export type AuditAction =
  | "TICKET"
  | "RULE_PROPOSED"
  | "RULE_ACTIVATED"
  | "RULE_DECLINED"
  | "VIEW"
  | "HELP"
  | "REJECTED_CHAT"
  | "CAP_REACHED"
  | "BAD_INPUT"
  | "DISPATCH"
  | "ERROR";
export type AuditOutcome = "OK" | "REFUSED" | "ERROR";

export interface RuleRecord {
  rule_id: string;
  version: number;
  structure: RuleV1;
  source_text: string;
}

export interface IntakeInsert {
  updateId: number;
  ticketDate: string;
  sourceKind: "IMAGE" | "PDF";
  bookmaker: string | null;
  model: string;
  rowsRead: number;
  rowsKept: number;
  rowsMatched: number;
}

export interface TicketPick {
  home: string;
  away: string;
  arm: string;
  pick: string;
  adjustment: string | null;
  forced: boolean;
}

export interface TicketSummary {
  kind: string;
  fixtureCount: number;
  picks: TicketPick[];
}

export interface ScoreRow {
  ticket_kind: string;
  arm: string;
  tickets: number;
  picks_settled: number;
  picks_right: number;
}

export interface IntakeStore {
  /** False when this Telegram update id was already handled (a retried delivery). */
  claimUpdate(updateId: number, kind: UpdateKind): Promise<boolean>;
  /** Takes one vision call for the UTC day; false when the cap is already reached. */
  reserveCall(date: string, cap: number): Promise<boolean>;
  findFixtures(fromIso: string, toIso: string): Promise<StoredFixture[]>;
  saveIntake(ticket: IntakeInsert, rows: readonly MatchedRow[]): Promise<string>;
  audit(action: AuditAction, outcome: AuditOutcome, detail: string | null, updateId: number | null): Promise<void>;
  activeRule(): Promise<RuleRecord | null>;
  pendingRule(): Promise<RuleRecord | null>;
  proposeRule(structure: RuleV1, sourceText: string, updateId: number): Promise<RuleRecord>;
  activateRule(rule: RuleRecord, previous: RuleRecord | null, updateId: number): Promise<void>;
  declineRule(rule: RuleRecord, updateId: number): Promise<void>;
  ticketsForDate(date: string): Promise<TicketSummary[]>;
  scoreboard(): Promise<ScoreRow[]>;
}

export class StoreError extends Error {
  constructor(readonly status: number | null) {
    super(`store request failed${status === null ? "" : ` (${status})`}`);
  }
}

type Query = readonly (readonly [string, string])[];

export class PostgrestStore implements IntakeStore {
  constructor(
    private readonly baseUrl: string,
    private readonly serviceKey: string,
    private readonly fetchFn: FetchLike = fetch,
    private readonly schema = "proracle",
  ) {}

  private async rest(
    method: "GET" | "POST",
    path: string,
    opts: { query?: Query; body?: unknown; prefer?: string } = {},
  ): Promise<unknown> {
    const qs = opts.query && opts.query.length > 0
      ? "?" + opts.query.map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(v).replace(/%2C/g, ",").replace(/%28/g, "(").replace(/%29/g, ")").replace(/%3A/g, ":")}`).join("&")
      : "";
    const headers: Record<string, string> = {
      apikey: this.serviceKey,
      authorization: `Bearer ${this.serviceKey}`,
      accept: "application/json",
      "accept-profile": this.schema,
    };
    if (opts.body !== undefined) {
      headers["content-type"] = "application/json";
      headers["content-profile"] = this.schema;
    }
    if (opts.prefer) headers["prefer"] = opts.prefer;
    let response: Response;
    try {
      response = await this.fetchFn(`${this.baseUrl}/rest/v1/${path}${qs}`, {
        method,
        headers,
        body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
        signal: AbortSignal.timeout(20_000),
      });
    } catch {
      throw new StoreError(null);
    }
    if (!response.ok) {
      await response.body?.cancel();
      throw new StoreError(response.status);
    }
    const text = await response.text();
    return text.length === 0 ? null : JSON.parse(text);
  }

  async claimUpdate(updateId: number, kind: UpdateKind): Promise<boolean> {
    const rows = await this.rest("POST", "intake_updates", {
      query: [["on_conflict", "update_id"]],
      body: { update_id: updateId, kind },
      prefer: "resolution=ignore-duplicates,return=representation",
    });
    return Array.isArray(rows) && rows.length > 0;
  }

  async reserveCall(date: string, cap: number): Promise<boolean> {
    const result = await this.rest("POST", "rpc/intake_reserve_call", { body: { p_date: date, p_cap: cap } });
    return result === true;
  }

  async findFixtures(fromIso: string, toIso: string): Promise<StoredFixture[]> {
    const rows = await this.rest("GET", "fixtures", {
      query: [
        ["select", "id,home_team,away_team,kickoff_at,competition_name"],
        ["status", "eq.SCHEDULED"],
        ["kickoff_at", `gte.${fromIso}`],
        ["kickoff_at", `lt.${toIso}`],
        ["order", "kickoff_at.asc"],
        ["limit", "1000"],
      ],
    });
    return Array.isArray(rows) ? (rows as StoredFixture[]) : [];
  }

  async saveIntake(t: IntakeInsert, rows: readonly MatchedRow[]): Promise<string> {
    const created = await this.rest("POST", "intake_tickets", {
      body: {
        update_id: t.updateId,
        ticket_date: t.ticketDate,
        source_kind: t.sourceKind,
        bookmaker_label: t.bookmaker,
        vision_model: t.model,
        rows_read: t.rowsRead,
        rows_kept: t.rowsKept,
        rows_matched: t.rowsMatched,
      },
      prefer: "return=representation",
    });
    const id = Array.isArray(created) ? (created[0] as { id?: unknown } | undefined)?.id : undefined;
    if (typeof id !== "string") throw new StoreError(null);
    if (rows.length > 0) {
      await this.rest("POST", "intake_ticket_rows", {
        body: rows.map((m, i) => ({
          intake_id: id,
          position: i + 1,
          home_team: m.row.home_team,
          away_team: m.row.away_team,
          kickoff_at: m.row.kickoff_at,
          home_odds: m.row.home_odds,
          draw_odds: m.row.draw_odds,
          away_odds: m.row.away_odds,
          matched_fixture_id: m.fixtureId,
        })),
        prefer: "return=minimal",
      });
    }
    return id;
  }

  async audit(action: AuditAction, outcome: AuditOutcome, detail: string | null, updateId: number | null): Promise<void> {
    await this.rest("POST", "intake_audit", {
      body: { update_id: updateId, action, outcome, detail: detail === null ? null : detail.slice(0, 200) },
      prefer: "return=minimal",
    });
  }

  private async oneRule(view: string): Promise<RuleRecord | null> {
    const rows = await this.rest("GET", view, { query: [["select", "rule_id,version,structure,source_text"], ["limit", "1"]] });
    return Array.isArray(rows) && rows.length > 0 ? (rows[0] as RuleRecord) : null;
  }

  activeRule(): Promise<RuleRecord | null> {
    return this.oneRule("intake_active_rule");
  }

  pendingRule(): Promise<RuleRecord | null> {
    return this.oneRule("intake_pending_rule");
  }

  async proposeRule(structure: RuleV1, sourceText: string, updateId: number): Promise<RuleRecord> {
    const latest = await this.rest("GET", "intake_rules", {
      query: [["select", "version"], ["order", "version.desc"], ["limit", "1"]],
    });
    const last = Array.isArray(latest) && latest.length > 0 ? Number((latest[0] as { version: unknown }).version) : 0;
    const created = await this.rest("POST", "intake_rules", {
      body: { version: last + 1, structure, source_text: sourceText, proposed_update_id: updateId },
      prefer: "return=representation",
    });
    const row = Array.isArray(created) ? (created[0] as { id?: unknown } | undefined) : undefined;
    if (typeof row?.id !== "string") throw new StoreError(null);
    await this.rest("POST", "intake_rule_events", {
      body: { rule_id: row.id, event: "PROPOSED", update_id: updateId },
      prefer: "return=minimal",
    });
    return { rule_id: row.id, version: last + 1, structure, source_text: sourceText };
  }

  async activateRule(rule: RuleRecord, previous: RuleRecord | null, updateId: number): Promise<void> {
    await this.rest("POST", "intake_rule_events", {
      body: { rule_id: rule.rule_id, event: "ACTIVATED", update_id: updateId },
      prefer: "return=minimal",
    });
    if (previous && previous.rule_id !== rule.rule_id) {
      await this.rest("POST", "intake_rule_events", {
        body: { rule_id: previous.rule_id, event: "RETIRED", update_id: updateId },
        prefer: "return=minimal",
      });
    }
  }

  async declineRule(rule: RuleRecord, updateId: number): Promise<void> {
    await this.rest("POST", "intake_rule_events", {
      body: { rule_id: rule.rule_id, event: "DECLINED", update_id: updateId },
      prefer: "return=minimal",
    });
  }

  async ticketsForDate(date: string): Promise<TicketSummary[]> {
    const tickets = await this.rest("GET", "tickets", {
      query: [["select", "id,ticket_kind,fixture_count"], ["ticket_date", `eq.${date}`], ["order", "created_at.asc"]],
    });
    if (!Array.isArray(tickets) || tickets.length === 0) return [];
    const list = tickets as { id: string; ticket_kind: string; fixture_count: number }[];
    const ids = list.map((t) => t.id).join(",");
    const picks = await this.rest("GET", "ticket_arm_picks", {
      query: [["select", "ticket_id,fixture_id,arm,pick,adjustment,forced"], ["ticket_id", `in.(${ids})`], ["order", "created_at.asc"]],
    });
    const pickRows = (Array.isArray(picks) ? picks : []) as {
      ticket_id: string;
      fixture_id: string;
      arm: string;
      pick: string;
      adjustment: string | null;
      forced: boolean;
    }[];
    const fixtureIds = [...new Set(pickRows.map((p) => p.fixture_id))];
    const names = new Map<string, { home: string; away: string }>();
    if (fixtureIds.length > 0) {
      const fixtures = await this.rest("GET", "fixtures", {
        query: [["select", "id,home_team,away_team"], ["id", `in.(${fixtureIds.join(",")})`]],
      });
      for (const f of (Array.isArray(fixtures) ? fixtures : []) as { id: string; home_team: string; away_team: string }[]) {
        names.set(f.id, { home: f.home_team, away: f.away_team });
      }
    }
    return list.map((t) => ({
      kind: t.ticket_kind,
      fixtureCount: t.fixture_count,
      picks: pickRows
        .filter((p) => p.ticket_id === t.id)
        .map((p) => ({
          home: names.get(p.fixture_id)?.home ?? "?",
          away: names.get(p.fixture_id)?.away ?? "?",
          arm: p.arm,
          pick: p.pick,
          adjustment: p.adjustment,
          forced: p.forced,
        })),
    }));
  }

  async scoreboard(): Promise<ScoreRow[]> {
    const rows = await this.rest("GET", "intake_arm_scoreboard", { query: [["select", "*"]] });
    return Array.isArray(rows) ? (rows as ScoreRow[]) : [];
  }
}
