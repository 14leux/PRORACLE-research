// Database access for the intake: an interface (so tests use an in-memory fake) and the PostgREST
// implementation. It touches only the proracle intake and ticket tables, as service_role, and never
// stores an image or message text.

import type { RuleV1 } from "./rules.ts";
import type { MatchedHistoryRow, MatchedRow, StoredFixture } from "./matching.ts";
import type { FetchLike, ParsedRow, RejectedRow } from "./vision.ts";

export type UpdateKind = "IMAGE" | "PDF" | "COMMAND" | "TEXT" | "REJECTED";
export type AuditAction =
  | "TICKET"
  | "RULE_PROPOSED"
  | "RULE_ACTIVATED"
  | "RULE_DECLINED"
  | "RULE_RETIRE_REQUESTED"
  | "RULE_RETIRED"
  | "VIEW"
  | "HELP"
  | "REJECTED_CHAT"
  | "CAP_REACHED"
  | "BAD_INPUT"
  | "DISPATCH"
  | "ERROR"
  | "SLATE"
  | "PENDING"
  | "REUSED"
  | "TOKEN_CAP"
  | "ELI5";
export type AuditOutcome = "OK" | "REFUSED" | "ERROR";

export interface RuleRecord {
  rule_id: string;
  version: number;
  structure: RuleV1;
  source_text: string;
}

/** What a stored read is: a declared slip (arms frozen) or a slate (kept for testing models, nothing frozen). */
export type ReadKind = "TICKET" | "SLATE";

/** The one upload waiting for the answer to "ticket or slate?": a Telegram file id and its facts, never the file. */
export interface PendingUpload {
  id: string;
  updateId: number;
  fileId: string;
  fileType: "IMAGE" | "PDF";
  fileSize: number | null;
  /** ISO time after which the upload no longer counts (60 minutes after it was sent). */
  expiresAt: string;
}

/** An earlier read found again by the SHA-256 of the file's bytes: what was stored from it, used instead of a new read. */
export interface StoredRead {
  intakeId: string;
  bookmaker: string | null;
  rowsRead: number;
  rows: ParsedRow[];
  rejected: RejectedRow[];
}

/** Today's use of the vision call, for the call cap and the token budget. */
export interface DayUsage {
  calls: number;
  tokens: number;
}

export interface IntakeInsert {
  updateId: number;
  kind: ReadKind;
  /** Vision tokens for this read; null for a read that made no call (a reused one) or whose response gave no count. */
  inputTokens: number | null;
  outputTokens: number | null;
  /** The pending upload this read answers, used up by it; null when the caption named the kind. */
  pendingUploadId: string | null;
  /** SHA-256 (hex) of the file's bytes, so an identical repeat can reuse this read. */
  fileSha256: string | null;
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
  /** "rule-v<N>" for arm B, otherwise the model or arm name. */
  armVersion: string;
  pick: string;
  adjustment: string | null;
  forced: boolean;
}

export interface TicketSummary {
  kind: string;
  fixtureCount: number;
  picks: TicketPick[];
}

/** One arm (arm B once per rule version) on settled picks of complete tickets, from intake_scoreboard. */
export interface ScoreRow {
  ticket_kind: string;
  arm: string;
  arm_version: string;
  tickets: number;
  picks_settled: number;
  picks_right: number;
  /** What arm A and arm C picked on exactly the same fixtures. */
  a_right_same_fixtures: number;
  c_right_same_fixtures: number;
  /** Picks where the arm differs from arm A by design (a forced draw or a reversal). */
  adjusted_settled: number;
  adjusted_right: number;
  a_right_on_adjusted: number;
}

/**
 * One arm's settled picks in one window of intake_window_summary: LAST7 is the last 7 days, LAST28 the last 28
 * (it contains LAST7), PRIOR28 the 28 before those. Counts only, on complete tickets, with what arm A picked
 * on exactly the same fixtures.
 */
export interface WindowRow {
  ticket_kind: string;
  arm: string;
  arm_version: string;
  window_name: "LAST7" | "LAST28" | "PRIOR28";
  picks_settled: number;
  picks_right: number;
  a_right_same_fixtures: number;
}

/** One complete ticket's arm in intake_ticket_hits: settled and right picks, and the latest settled kickoff. */
export interface TicketHitRow {
  ticket_id: string;
  ticket_kind: string;
  ticket_date: string;
  arm: string;
  arm_version: string;
  fixture_count: number;
  picks_settled: number;
  picks_right: number;
  last_kickoff_at: string;
}

/** The one row of intake_slate_score: counts of settled slate fixtures, each counted once. */
export interface SlateScoreRow {
  slate_reads: number;
  fixtures: number;
  settled: number;
  model_hits: number;
  /** Settled fixtures where all three prices were printed, and how often the model and the market favourite were right on them. */
  compared: number;
  compared_model_hits: number;
  compared_market_hits: number;
}

export interface IntakeStore {
  /** False when this Telegram update id was already handled (a retried delivery). */
  claimUpdate(updateId: number, kind: UpdateKind): Promise<boolean>;
  /** Takes one vision call for the UTC day; false when the cap is already reached. */
  reserveCall(date: string, cap: number): Promise<boolean>;
  findFixtures(fromIso: string, toIso: string): Promise<StoredFixture[]>;
  /** The newest ticket rows that matched a stored fixture, with that fixture's names: the matching memory. */
  matchedHistory(): Promise<MatchedHistoryRow[]>;
  saveIntake(ticket: IntakeInsert, rows: readonly MatchedRow[], rejected: readonly RejectedRow[]): Promise<string>;
  /** Keeps the Telegram file id of an upload that is waiting for "ticket or slate?" (60 minutes; no file). */
  savePending(upload: { updateId: number; fileId: string; fileType: "IMAGE" | "PDF"; fileSize: number | null }): Promise<void>;
  /** The newest upload still waiting: not expired and not yet read. Null when there is none. */
  latestPending(): Promise<PendingUpload | null>;
  /** The latest earlier read made from a file with this SHA-256, or null. */
  findRead(sha256: string): Promise<StoredRead | null>;
  usageToday(date: string): Promise<DayUsage>;
  /** Adds one read's tokens to the UTC day's totals. */
  addTokens(date: string, input: number, output: number): Promise<void>;
  audit(action: AuditAction, outcome: AuditOutcome, detail: string | null, updateId: number | null): Promise<void>;
  /** Every rule in force (up to five arm B pick sets), oldest version first. */
  activeRules(): Promise<RuleRecord[]>;
  pendingRule(): Promise<RuleRecord | null>;
  /** The active rule whose retirement waits for YES or NO, if any. */
  pendingRetirement(): Promise<RuleRecord | null>;
  proposeRule(structure: RuleV1, sourceText: string, updateId: number): Promise<RuleRecord>;
  /** Adds the rule to the active set; no other rule is touched. */
  activateRule(rule: RuleRecord, updateId: number): Promise<void>;
  declineRule(rule: RuleRecord, updateId: number): Promise<void>;
  requestRetirement(rule: RuleRecord, updateId: number): Promise<void>;
  confirmRetirement(rule: RuleRecord, updateId: number): Promise<void>;
  declineRetirement(rule: RuleRecord, updateId: number): Promise<void>;
  ticketsForDate(date: string): Promise<TicketSummary[]>;
  scoreboard(): Promise<ScoreRow[]>;
  /** The last 7 days, the last 28 and the 28 before, per arm and ticket kind (the view intake_window_summary). */
  windowSummary(): Promise<WindowRow[]>;
  /** Settled and right picks per complete ticket and arm (the view intake_ticket_hits), for ticket spikes. */
  ticketHits(): Promise<TicketHitRow[]>;
  /** Settled slate counts (the view intake_slate_score); null while there is no row to read. */
  slateScore(): Promise<SlateScoreRow | null>;
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

  async matchedHistory(): Promise<MatchedHistoryRow[]> {
    const rows = await this.rest("GET", "intake_ticket_rows", {
      query: [
        ["select", "home_team,away_team,matched:fixtures!matched_fixture_id(home_team,away_team)"],
        ["matched_fixture_id", "not.is.null"],
        ["order", "observed_at.desc"],
        ["limit", "2000"],
      ],
    });
    return Array.isArray(rows) ? (rows as MatchedHistoryRow[]) : [];
  }

  async savePending(u: { updateId: number; fileId: string; fileType: "IMAGE" | "PDF"; fileSize: number | null }): Promise<void> {
    await this.rest("POST", "intake_pending_uploads", {
      body: { update_id: u.updateId, file_id: u.fileId, file_type: u.fileType, file_size: u.fileSize },
      prefer: "return=minimal",
    });
  }

  async latestPending(): Promise<PendingUpload | null> {
    const rows = await this.rest("GET", "intake_pending_upload", {
      query: [["select", "id,update_id,file_id,file_type,file_size,expires_at"], ["limit", "1"]],
    });
    const r = Array.isArray(rows)
      ? (rows[0] as { id: string; update_id: number; file_id: string; file_type: string; file_size: number | null; expires_at: string } | undefined)
      : undefined;
    if (!r || (r.file_type !== "IMAGE" && r.file_type !== "PDF")) return null;
    return {
      id: r.id,
      updateId: Number(r.update_id),
      fileId: r.file_id,
      fileType: r.file_type,
      fileSize: r.file_size === null ? null : Number(r.file_size),
      expiresAt: new Date(r.expires_at).toISOString(),
    };
  }

  async findRead(sha256: string): Promise<StoredRead | null> {
    const hashes = await this.rest("GET", "intake_file_hashes", {
      query: [["select", "intake_id"], ["file_sha256", `eq.${sha256}`], ["order", "created_at.desc"], ["limit", "1"]],
    });
    const intakeId = Array.isArray(hashes) ? (hashes[0] as { intake_id?: unknown } | undefined)?.intake_id : undefined;
    if (typeof intakeId !== "string") return null;
    const [head, rows, rejected] = await Promise.all([
      this.rest("GET", "intake_tickets", {
        query: [["select", "bookmaker_label,rows_read"], ["id", `eq.${intakeId}`], ["limit", "1"]],
      }),
      this.rest("GET", "intake_ticket_rows", {
        query: [
          ["select", "home_team,away_team,kickoff_at,home_odds,draw_odds,away_odds"],
          ["intake_id", `eq.${intakeId}`],
          ["order", "position.asc"],
          ["limit", "100"],
        ],
      }),
      this.rest("GET", "intake_rejected_rows", {
        query: [
          ["select", "position,home_team,away_team,reason"],
          ["intake_id", `eq.${intakeId}`],
          ["order", "position.asc"],
          ["limit", "500"],
        ],
      }),
    ]);
    const h = Array.isArray(head) ? (head[0] as { bookmaker_label: string | null; rows_read: number } | undefined) : undefined;
    if (!h || !Array.isArray(rows) || rows.length === 0) return null;
    const num = (v: unknown): number | null => (v === null || v === undefined ? null : Number(v));
    return {
      intakeId,
      bookmaker: h.bookmaker_label,
      rowsRead: Number(h.rows_read),
      rows: (rows as Record<string, unknown>[]).map((r) => ({
        home_team: String(r.home_team),
        away_team: String(r.away_team),
        kickoff_at: typeof r.kickoff_at === "string" ? new Date(r.kickoff_at).toISOString() : null,
        home_odds: num(r.home_odds),
        draw_odds: num(r.draw_odds),
        away_odds: num(r.away_odds),
      })),
      rejected: (Array.isArray(rejected) ? (rejected as Record<string, unknown>[]) : []).map((r) => ({
        position: Number(r.position),
        home: typeof r.home_team === "string" ? r.home_team : null,
        away: typeof r.away_team === "string" ? r.away_team : null,
        reason: r.reason as RejectedRow["reason"],
      })),
    };
  }

  async usageToday(date: string): Promise<DayUsage> {
    const rows = await this.rest("GET", "intake_usage", {
      query: [["select", "vision_calls,input_tokens,output_tokens"], ["usage_date", `eq.${date}`], ["limit", "1"]],
    });
    const r = Array.isArray(rows)
      ? (rows[0] as { vision_calls: number; input_tokens: number; output_tokens: number } | undefined)
      : undefined;
    return r ? { calls: Number(r.vision_calls), tokens: Number(r.input_tokens) + Number(r.output_tokens) } : { calls: 0, tokens: 0 };
  }

  async addTokens(date: string, input: number, output: number): Promise<void> {
    await this.rest("POST", "rpc/intake_add_tokens", { body: { p_date: date, p_input: input, p_output: output } });
  }

  async saveIntake(t: IntakeInsert, rows: readonly MatchedRow[], rejected: readonly RejectedRow[]): Promise<string> {
    const created = await this.rest("POST", "intake_tickets", {
      body: {
        update_id: t.updateId,
        kind: t.kind,
        input_tokens: t.inputTokens,
        output_tokens: t.outputTokens,
        pending_upload_id: t.pendingUploadId,
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
          read_kind: t.kind,
          match_via: m.fixtureId === null ? null : (m.via ?? null),
          unmatched_reason: m.fixtureId === null ? m.reason : null,
        })),
        prefer: "return=minimal",
      });
    }
    if (rejected.length > 0) {
      await this.rest("POST", "intake_rejected_rows", {
        body: rejected.map((r) => ({ intake_id: id, position: r.position, home_team: r.home, away_team: r.away, reason: r.reason })),
        prefer: "return=minimal",
      });
    }
    if (t.fileSha256 !== null) {
      await this.rest("POST", "intake_file_hashes", {
        body: { file_sha256: t.fileSha256, intake_id: id },
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

  async activeRules(): Promise<RuleRecord[]> {
    const rows = await this.rest("GET", "intake_active_rules", {
      query: [["select", "rule_id,version,structure,source_text"], ["order", "version.asc"], ["limit", "20"]],
    });
    return Array.isArray(rows) ? (rows as RuleRecord[]) : [];
  }

  pendingRule(): Promise<RuleRecord | null> {
    return this.oneRule("intake_pending_rule");
  }

  pendingRetirement(): Promise<RuleRecord | null> {
    return this.oneRule("intake_pending_retirement");
  }

  private async ruleEvent(rule: RuleRecord, event: string, updateId: number): Promise<void> {
    await this.rest("POST", "intake_rule_events", {
      body: { rule_id: rule.rule_id, event, update_id: updateId },
      prefer: "return=minimal",
    });
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

  activateRule(rule: RuleRecord, updateId: number): Promise<void> {
    return this.ruleEvent(rule, "ACTIVATED", updateId);
  }

  declineRule(rule: RuleRecord, updateId: number): Promise<void> {
    return this.ruleEvent(rule, "DECLINED", updateId);
  }

  requestRetirement(rule: RuleRecord, updateId: number): Promise<void> {
    return this.ruleEvent(rule, "RETIRE_REQUESTED", updateId);
  }

  confirmRetirement(rule: RuleRecord, updateId: number): Promise<void> {
    return this.ruleEvent(rule, "RETIRED", updateId);
  }

  declineRetirement(rule: RuleRecord, updateId: number): Promise<void> {
    return this.ruleEvent(rule, "RETIRE_DECLINED", updateId);
  }

  async ticketsForDate(date: string): Promise<TicketSummary[]> {
    const tickets = await this.rest("GET", "tickets", {
      query: [["select", "id,ticket_kind,fixture_count"], ["ticket_date", `eq.${date}`], ["order", "created_at.asc"]],
    });
    if (!Array.isArray(tickets) || tickets.length === 0) return [];
    const list = tickets as { id: string; ticket_kind: string; fixture_count: number }[];
    const ids = list.map((t) => t.id).join(",");
    const picks = await this.rest("GET", "ticket_arm_picks", {
      query: [["select", "ticket_id,fixture_id,arm,arm_version,pick,adjustment,forced"], ["ticket_id", `in.(${ids})`], ["order", "created_at.asc"]],
    });
    const pickRows = (Array.isArray(picks) ? picks : []) as {
      ticket_id: string;
      fixture_id: string;
      arm: string;
      arm_version: string;
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
          armVersion: p.arm_version,
          pick: p.pick,
          adjustment: p.adjustment,
          forced: p.forced,
        })),
    }));
  }

  async scoreboard(): Promise<ScoreRow[]> {
    const rows = await this.rest("GET", "intake_scoreboard", { query: [["select", "*"]] });
    return Array.isArray(rows) ? (rows as ScoreRow[]) : [];
  }

  async windowSummary(): Promise<WindowRow[]> {
    const rows = await this.rest("GET", "intake_window_summary", { query: [["select", "*"]] });
    return Array.isArray(rows) ? (rows as WindowRow[]) : [];
  }

  async ticketHits(): Promise<TicketHitRow[]> {
    const rows = await this.rest("GET", "intake_ticket_hits", { query: [["select", "*"]] });
    return Array.isArray(rows) ? (rows as TicketHitRow[]) : [];
  }

  async slateScore(): Promise<SlateScoreRow | null> {
    const rows = await this.rest("GET", "intake_slate_score", { query: [["select", "*"]] });
    return Array.isArray(rows) && rows.length > 0 ? (rows[0] as SlateScoreRow) : null;
  }
}
