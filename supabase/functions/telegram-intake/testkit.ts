// Test doubles shared by the Deno tests: an in-memory store, a recording Telegram client, and
// builders for webhook requests. No network, no clock, no secrets (the values below are placeholders).

import type { Deps } from "./handler.ts";
import { DEFAULT_TOKEN_BUDGET } from "./commands.ts";
import type { MatchedHistoryRow, MatchedRow, StoredFixture } from "./matching.ts";
import type {
  AuditAction,
  AuditOutcome,
  DayUsage,
  IntakeInsert,
  PendingUpload,
  StoredRead,
  IntakeStore,
  RuleRecord,
  RecentRow,
  ScoreRow,
  SlateScoreRow,
  TicketSummary,
  UpdateKind,
} from "./store.ts";
import type { RuleV1 } from "./rules.ts";
import type { RejectedRow, TokenUsage, VisionResult } from "./vision.ts";
import { type MenuEntry, type TelegramApi, TelegramError } from "./telegram.ts";

export const SECRET = "placeholder-webhook-secret";
export const ALLOWED = "424242";
export const NOW = new Date("2026-11-26T09:00:00Z");

export class FakeStore implements IntakeStore {
  claimed = new Set<number>();
  claimKinds: UpdateKind[] = [];
  calls = new Map<string, number>();
  fixtures: StoredFixture[] = [];
  intakes: { ticket: IntakeInsert; rows: MatchedRow[]; rejected: RejectedRow[]; id: string }[] = [];
  /** Uploads waiting for the ticket-or-slate answer, oldest first; the last unexpired, unused one is offered. */
  pendings: (PendingUpload & { createdAt: number })[] = [];
  /** Tokens added to each UTC day, and what findRead returns for a SHA-256. */
  tokenTotals = new Map<string, number>();
  reads = new Map<string, StoredRead>();
  /** When a pending upload was sent, for its 60-minute expiry. */
  pendingNow = NOW.getTime();
  audits: { action: AuditAction; outcome: AuditOutcome; detail: string | null; updateId: number | null }[] = [];
  rules: RuleRecord[] = [];
  /** The rules in force, oldest version first. */
  active: RuleRecord[] = [];
  pending: RuleRecord | null = null;
  retiring: RuleRecord | null = null;
  events: string[] = [];
  tickets: TicketSummary[] = [];
  score: ScoreRow[] = [];
  recent: RecentRow[] = [];
  slate: SlateScoreRow | null = null;
  failOn: string | null = null;
  touched = 0;

  private guard(name: string) {
    this.touched++;
    if (this.failOn === name) throw new Error(`forced failure in ${name}`);
  }

  claimUpdate(updateId: number, kind: UpdateKind): Promise<boolean> {
    this.guard("claimUpdate");
    if (this.claimed.has(updateId)) return Promise.resolve(false);
    this.claimed.add(updateId);
    this.claimKinds.push(kind);
    return Promise.resolve(true);
  }

  reserveCall(date: string, cap: number): Promise<boolean> {
    this.guard("reserveCall");
    const used = this.calls.get(date) ?? 0;
    if (used >= cap) return Promise.resolve(false);
    this.calls.set(date, used + 1);
    return Promise.resolve(true);
  }

  findFixtures(): Promise<StoredFixture[]> {
    this.guard("findFixtures");
    return Promise.resolve(this.fixtures);
  }

  history: MatchedHistoryRow[] = [];
  matchedHistory(): Promise<MatchedHistoryRow[]> {
    this.guard("matchedHistory");
    return Promise.resolve(this.history);
  }

  saveIntake(ticket: IntakeInsert, rows: readonly MatchedRow[], rejected: readonly RejectedRow[]): Promise<string> {
    this.guard("saveIntake");
    const id = `intake-${this.intakes.length + 1}`;
    if (ticket.pendingUploadId !== null && this.intakes.some((i) => i.ticket.pendingUploadId === ticket.pendingUploadId)) {
      return Promise.reject(new Error("a pending upload is used once"));
    }
    this.intakes.push({ ticket, rows: [...rows], rejected: [...rejected], id });
    if (ticket.fileSha256 !== null) {
      this.reads.set(ticket.fileSha256, {
        intakeId: id,
        bookmaker: ticket.bookmaker,
        rowsRead: ticket.rowsRead,
        rows: rows.map((r) => r.row),
        rejected: [...rejected],
      });
    }
    return Promise.resolve(id);
  }

  savePending(upload: { updateId: number; fileId: string; fileType: "IMAGE" | "PDF"; fileSize: number | null }): Promise<void> {
    this.guard("savePending");
    this.pendings.push({
      id: `pending-${this.pendings.length + 1}`,
      ...upload,
      createdAt: this.pendingNow,
      expiresAt: new Date(this.pendingNow + 60 * 60_000).toISOString(),
    });
    return Promise.resolve();
  }

  /** Like the database view: only the newest upload, and only if it has not been read yet (the handler checks expiry). */
  latestPending(): Promise<PendingUpload | null> {
    this.guard("latestPending");
    const p = this.pendings.at(-1);
    return Promise.resolve(p !== undefined && !this.intakes.some((i) => i.ticket.pendingUploadId === p.id) ? p : null);
  }

  findRead(sha256: string): Promise<StoredRead | null> {
    this.guard("findRead");
    return Promise.resolve(this.reads.get(sha256) ?? null);
  }

  usageToday(date: string): Promise<DayUsage> {
    this.guard("usageToday");
    return Promise.resolve({ calls: this.calls.get(date) ?? 0, tokens: this.tokenTotals.get(date) ?? 0 });
  }

  addTokens(date: string, input: number, output: number): Promise<void> {
    this.guard("addTokens");
    this.tokenTotals.set(date, (this.tokenTotals.get(date) ?? 0) + input + output);
    return Promise.resolve();
  }

  audit(action: AuditAction, outcome: AuditOutcome, detail: string | null, updateId: number | null): Promise<void> {
    this.touched++;
    this.audits.push({ action, outcome, detail, updateId });
    return Promise.resolve();
  }

  activeRules() {
    this.guard("activeRules");
    return Promise.resolve([...this.active].sort((a, b) => a.version - b.version));
  }

  pendingRule() {
    this.guard("pendingRule");
    return Promise.resolve(this.pending);
  }

  pendingRetirement() {
    this.guard("pendingRetirement");
    return Promise.resolve(this.retiring);
  }

  proposeRule(structure: RuleV1, sourceText: string): Promise<RuleRecord> {
    this.guard("proposeRule");
    const rule = { rule_id: `rule-${this.rules.length + 1}`, version: this.rules.length + 1, structure, source_text: sourceText };
    this.rules.push(rule);
    this.pending = rule;
    this.events.push(`PROPOSED v${rule.version}`);
    return Promise.resolve(rule);
  }

  activateRule(rule: RuleRecord): Promise<void> {
    this.guard("activateRule");
    this.active = [...this.active, rule];
    this.pending = null;
    this.events.push(`ACTIVATED v${rule.version}`);
    return Promise.resolve();
  }

  requestRetirement(rule: RuleRecord): Promise<void> {
    this.guard("requestRetirement");
    this.retiring = rule;
    this.events.push(`RETIRE_REQUESTED v${rule.version}`);
    return Promise.resolve();
  }

  confirmRetirement(rule: RuleRecord): Promise<void> {
    this.guard("confirmRetirement");
    this.active = this.active.filter((r) => r.rule_id !== rule.rule_id);
    this.retiring = null;
    this.events.push(`RETIRED v${rule.version}`);
    return Promise.resolve();
  }

  declineRetirement(rule: RuleRecord): Promise<void> {
    this.guard("declineRetirement");
    this.retiring = null;
    this.events.push(`RETIRE_DECLINED v${rule.version}`);
    return Promise.resolve();
  }

  declineRule(rule: RuleRecord): Promise<void> {
    this.guard("declineRule");
    this.pending = null;
    this.events.push(`DECLINED v${rule.version}`);
    return Promise.resolve();
  }

  ticketsForDate(): Promise<TicketSummary[]> {
    this.guard("ticketsForDate");
    return Promise.resolve(this.tickets);
  }

  scoreboard(): Promise<ScoreRow[]> {
    this.guard("scoreboard");
    return Promise.resolve(this.score);
  }

  recentWindow(): Promise<RecentRow[]> {
    this.guard("recentWindow");
    return Promise.resolve(this.recent);
  }

  slateScore(): Promise<SlateScoreRow | null> {
    this.guard("slateScore");
    return Promise.resolve(this.slate);
  }
}

export class FakeTelegram implements TelegramApi {
  sent: { chatId: string; text: string }[] = [];
  files = new Map<string, Uint8Array | null>();
  downloads: string[] = [];
  /** Each syncMenu call's argument; set `menuFails` to make the call throw. */
  menuSyncs: (readonly MenuEntry[])[] = [];
  menuFails = false;
  menuFailReason = "HTTP 400 Bad Request: test";
  syncMenu(menu: readonly MenuEntry[]): Promise<"UNCHANGED" | "UPDATED"> {
    this.menuSyncs.push(menu);
    return this.menuFails ? Promise.reject(new TelegramError(this.menuFailReason)) : Promise.resolve("UPDATED");
  }
  send(chatId: string, text: string): Promise<void> {
    this.sent.push({ chatId, text });
    return Promise.resolve();
  }
  download(fileId: string): Promise<Uint8Array | null> {
    this.downloads.push(fileId);
    return Promise.resolve(this.files.has(fileId) ? this.files.get(fileId)! : new Uint8Array([1, 2, 3]));
  }
  get texts(): string {
    return this.sent.map((s) => s.text).join("\n---\n");
  }
}

export interface Kit {
  deps: Deps;
  store: FakeStore;
  tg: FakeTelegram;
  visionCalls: unknown[];
  dispatched: string[];
  setVision: (output: unknown) => void;
  /** What the fake vision call reports it used; null means the response carried no usage. */
  setUsage: (usage: TokenUsage | null) => void;
  setDispatch: (ok: boolean) => void;
}

export function makeKit(overrides: Partial<Deps> = {}): Kit {
  const store = new FakeStore();
  const tg = new FakeTelegram();
  const visionCalls: unknown[] = [];
  const dispatched: string[] = [];
  let output: unknown = { fixtures: [] };
  let usage: TokenUsage | null = { input: 1200, output: 300 };
  let dispatchOk = true;
  const deps: Deps = {
    secretToken: SECRET,
    allowedChatId: ALLOWED,
    dailyCap: 10,
    dailyTokenBudget: DEFAULT_TOKEN_BUDGET,
    model: "claude-sonnet-5-5",
    utcOffsetMinutes: 180,
    anchorMatching: true,
    store,
    telegram: tg,
    vision: (body): Promise<VisionResult> => {
      visionCalls.push(body);
      return Promise.resolve({ output, usage });
    },
    dispatch: (id) => {
      dispatched.push(id);
      return Promise.resolve(dispatchOk);
    },
    now: () => NOW,
    ...overrides,
  };
  return {
    deps,
    store,
    tg,
    visionCalls,
    dispatched,
    setVision: (o) => {
      output = o;
    },
    setUsage: (u) => {
      usage = u;
    },
    setDispatch: (ok) => {
      dispatchOk = ok;
    },
  };
}

let nextId = 1000;

export function webhook(update: unknown, headers: Record<string, string> = { "x-telegram-bot-api-secret-token": SECRET }): Request {
  return new Request("https://example.test/telegram-intake", {
    method: "POST",
    headers: { "content-type": "application/json", ...headers },
    body: JSON.stringify(update),
  });
}

export function textMessage(text: string, opts: { updateId?: number; chatId?: number; fromId?: number; type?: string } = {}) {
  const chat = opts.chatId ?? Number(ALLOWED);
  return {
    update_id: opts.updateId ?? nextId++,
    message: { chat: { id: chat, type: opts.type ?? "private" }, from: { id: opts.fromId ?? chat }, text },
  };
}

/** A photo or file. The caption defaults to the word ticket (the marker that skips the question); pass caption: null to send none. */
export function fileMessage(
  kind: "photo" | { mime: string; size?: number },
  opts: { updateId?: number; size?: number; caption?: string | null } = {},
) {
  const chat = Number(ALLOWED);
  const caption = opts.caption === undefined ? "ticket" : opts.caption;
  const base = { chat: { id: chat, type: "private" }, from: { id: chat }, ...(caption === null ? {} : { caption }) };
  const message = kind === "photo"
    ? { ...base, photo: [{ file_id: "small", file_size: 100 }, { file_id: "photo-big", file_size: opts.size ?? 2000 }] }
    : { ...base, document: { file_id: "doc-1", mime_type: kind.mime, file_size: kind.size ?? 5000 } };
  return { update_id: opts.updateId ?? nextId++, message };
}

export function fixture(id: string, home: string, away: string, kickoff: string, competition = "League One"): StoredFixture {
  return { id, home_team: home, away_team: away, kickoff_at: kickoff, competition_name: competition };
}

export function row(home: string, away: string, time = "15:00", odds: [number, number, number] | null = [2.1, 3.3, 3.4]) {
  return {
    home_team: home,
    away_team: away,
    kickoff_date: "2026-11-26",
    kickoff_time: time,
    ...(odds ? { home_odds: odds[0], draw_odds: odds[1], away_odds: odds[2] } : {}),
  };
}
