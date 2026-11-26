// Test doubles shared by the Deno tests: an in-memory store, a recording Telegram client, and
// builders for webhook requests. No network, no clock, no secrets (the values below are placeholders).

import type { Deps } from "./handler.ts";
import type { MatchedHistoryRow, MatchedRow, StoredFixture } from "./matching.ts";
import type {
  AuditAction,
  AuditOutcome,
  IntakeInsert,
  IntakeStore,
  RuleRecord,
  ScoreRow,
  TicketSummary,
  UpdateKind,
} from "./store.ts";
import type { RuleV1 } from "./rules.ts";
import { type MenuEntry, type TelegramApi, TelegramError } from "./telegram.ts";

export const SECRET = "placeholder-webhook-secret";
export const ALLOWED = "424242";
export const NOW = new Date("2026-11-26T09:00:00Z");

export class FakeStore implements IntakeStore {
  claimed = new Set<number>();
  claimKinds: UpdateKind[] = [];
  calls = new Map<string, number>();
  fixtures: StoredFixture[] = [];
  intakes: { ticket: IntakeInsert; rows: MatchedRow[]; id: string }[] = [];
  audits: { action: AuditAction; outcome: AuditOutcome; detail: string | null; updateId: number | null }[] = [];
  rules: RuleRecord[] = [];
  /** The rules in force, oldest version first. */
  active: RuleRecord[] = [];
  pending: RuleRecord | null = null;
  retiring: RuleRecord | null = null;
  events: string[] = [];
  tickets: TicketSummary[] = [];
  score: ScoreRow[] = [];
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

  saveIntake(ticket: IntakeInsert, rows: readonly MatchedRow[]): Promise<string> {
    this.guard("saveIntake");
    const id = `intake-${this.intakes.length + 1}`;
    this.intakes.push({ ticket, rows: [...rows], id });
    return Promise.resolve(id);
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
  setDispatch: (ok: boolean) => void;
}

export function makeKit(overrides: Partial<Deps> = {}): Kit {
  const store = new FakeStore();
  const tg = new FakeTelegram();
  const visionCalls: unknown[] = [];
  const dispatched: string[] = [];
  let output: unknown = { fixtures: [] };
  let dispatchOk = true;
  const deps: Deps = {
    secretToken: SECRET,
    allowedChatId: ALLOWED,
    dailyCap: 10,
    model: "claude-sonnet-5-5",
    utcOffsetMinutes: 180,
    store,
    telegram: tg,
    vision: (body) => {
      visionCalls.push(body);
      return Promise.resolve(output);
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

export function fileMessage(
  kind: "photo" | { mime: string; size?: number },
  opts: { updateId?: number; size?: number } = {},
) {
  const chat = Number(ALLOWED);
  const base = { chat: { id: chat, type: "private" }, from: { id: chat } };
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
