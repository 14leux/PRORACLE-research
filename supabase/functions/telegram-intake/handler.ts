// The webhook handler. Order of checks: method, secret header (constant time), body size, the sender
// (one private chat only), retry deduplication, then the command. Every dependency is injected, so
// the tests run with fakes and no network.

import {
  ALLOWED_MEDIA,
  buildVisionRequest,
  parseVisionOutput,
  type ParsedRow,
  type ParsedTicket,
  type RejectedRow,
  type RejectReason,
  type TokenUsage,
  VisionError,
  type VisionResult,
} from "./vision.ts";
import {
  COMMANDS,
  helpText,
  MAX_ACTIVE_RULES,
  MAX_FILE_BYTES,
  MAX_FIXTURES,
  MAX_RULE_TEXT_CHARS,
  MENU,
  PENDING_RULE_MINUTES,
  PENDING_UPLOAD_MINUTES,
  SLATE_MAX_ROWS,
} from "./commands.ts";
import { buildEli5, marginPoints } from "./eli5.ts";
import { buildAliases, MATCH_WINDOW_MS, matchRows, type MatchedRow, seedAliases } from "./matching.ts";
import { canonicalRule, describeRule, parseRule } from "./rules.ts";
import type { AuditAction, AuditOutcome, DayUsage, IntakeStore, ReadKind, RuleRecord, ScoreRow, StoredRead, UpdateKind } from "./store.ts";
import { splitMessage, TelegramError, type TelegramApi } from "./telegram.ts";

export interface Deps {
  secretToken: string;
  allowedChatId: string;
  dailyCap: number;
  /** Vision tokens (input plus output) per UTC day before new reads are refused; next to the call cap. */
  dailyTokenBudget: number;
  model: string;
  utcOffsetMinutes: number;
  /** The one-team match (matching.ts, ANCHOR); on in production unless INTAKE_ANCHOR_MATCHING says off. */
  anchorMatching: boolean;
  store: IntakeStore;
  telegram: TelegramApi;
  /** One vision call: request body in, the forced tool's input and the tokens it used out. */
  vision: (body: ReturnType<typeof buildVisionRequest>) => Promise<VisionResult>;
  /** Starts the Python workflow for a stored intake; false when it could not be started. */
  dispatch: (intakeId: string) => Promise<boolean>;
  now: () => Date;
}

interface TgPhoto {
  file_id: string;
  file_size?: number;
}
interface TgDocument {
  file_id: string;
  mime_type?: string;
  file_size?: number;
}
interface TgMessage {
  chat: { id: number; type: string };
  from?: { id: number };
  text?: string;
  /** The caption Telegram sends with a photo or file: where the ticket or slate marker goes. */
  caption?: string;
  photo?: TgPhoto[];
  document?: TgDocument;
}
interface TgUpdate {
  update_id: number;
  message?: TgMessage;
}

const MAX_BODY_BYTES = 100_000;
const HOUR_MS = 3_600_000;

const text = (body: string, status = 200) => new Response(body, { status, headers: { "content-type": "text/plain" } });

async function sha256(value: string): Promise<Uint8Array> {
  return new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value)));
}

/** Constant-time comparison: both sides are hashed first, so length and content do not leak. */
export async function secretsEqual(a: string, b: string): Promise<boolean> {
  const [x, y] = await Promise.all([sha256(a), sha256(b)]);
  let diff = 0;
  for (let i = 0; i < x.length; i++) diff |= x[i] ^ y[i];
  return diff === 0;
}

function toBase64(bytes: Uint8Array): string {
  let binary = "";
  for (let i = 0; i < bytes.length; i += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  }
  return btoa(binary);
}

async function sha256Hex(bytes: Uint8Array): Promise<string> {
  const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", bytes as BufferSource));
  return [...digest].map((b) => b.toString(16).padStart(2, "0")).join("");
}

/** The media type from the file's first bytes, for an upload kept only as a Telegram file id (no declared type). */
function mediaTypeOf(type: "IMAGE" | "PDF", bytes: Uint8Array): string {
  const starts = (...b: number[]) => b.every((v, i) => bytes[i] === v);
  if (type === "PDF") return "application/pdf";
  if (starts(0x89, 0x50, 0x4e, 0x47)) return "image/png";
  if (starts(0x52, 0x49, 0x46, 0x46) && bytes[8] === 0x57 && bytes[9] === 0x45 && bytes[10] === 0x42 && bytes[11] === 0x50) {
    return "image/webp";
  }
  return "image/jpeg";
}

/** "ticket" or "slate" as a whole word in the caption; both words, or neither, means ask. */
export function captionKind(caption: string | undefined): ReadKind | null {
  const words = new Set((caption ?? "").toLowerCase().match(/[a-z]+/g) ?? []);
  const ticket = words.has("ticket");
  const slate = words.has("slate");
  return ticket === slate ? null : ticket ? "TICKET" : "SLATE";
}

const LABEL = { HOME_WIN: "home", DRAW: "draw", AWAY_WIN: "away" } as Record<string, string>;
const label = (pick: string) => LABEL[pick] ?? pick;

function pct(n: number, d: number): string {
  return d === 0 ? "n/a" : `${((100 * n) / d).toFixed(1)}%`;
}

/** "B3" for arm B of rule v3; arms A and C keep their letter. */
export const armLabel = (arm: string, version: string): string =>
  arm === "B" ? `B${version.replace(/^rule-v/, "")}` : arm;

/** A, then B2, B10 ... in version order (numerically), then C. */
export function compareArms(a: string, b: string): number {
  const x = a[0], y = b[0];
  if (x !== y) return x < y ? -1 : 1;
  return (Number(a.slice(1)) || 0) - (Number(b.slice(1)) || 0);
}

const versionsOf = (rules: readonly RuleRecord[]): string => rules.map((r) => `v${r.version}`).join(", ");

// A rough 95% margin of error for one hit rate (1.96 * sqrt(p(1-p)/n) with p = 0.5, in points).
const margin = (n: number): string => `±${marginPoints(n)} pts`;
const MIN_SETTLED_NOTE = 20;

type Route = (ctx: Ctx) => Promise<void>;
interface Ctx {
  deps: Deps;
  updateId: number;
  chatId: string;
  message: TgMessage;
  reply: (body: string) => Promise<void>;
  audit: (action: AuditAction, outcome: AuditOutcome, detail?: string) => Promise<void>;
}

// --- commands ---------------------------------------------------------------------------------

const help: Route = async (c) => {
  await c.audit("HELP", "OK");
  await c.reply(helpText(c.deps.dailyCap, c.deps.dailyTokenBudget));
};

const proposeRule: Route = async (c) => {
  const raw = (c.message.text ?? "").replace(/^\/rule(@\w+)?/i, "");
  if (raw.length > MAX_RULE_TEXT_CHARS + 20) {
    await c.audit("RULE_PROPOSED", "REFUSED", "too long");
    return c.reply(`Keep a rule under ${MAX_RULE_TEXT_CHARS} characters.`);
  }
  const parsed = parseRule(raw);
  if (!parsed.ok) {
    await c.audit("RULE_PROPOSED", "REFUSED", "not understood");
    return c.reply(parsed.error);
  }
  const [active, retiring] = await Promise.all([c.deps.store.activeRules(), c.deps.store.pendingRetirement()]);
  if (retiring) {
    await c.audit("RULE_PROPOSED", "REFUSED", "retirement pending");
    return c.reply(`Answer YES or NO to the retirement of rule v${retiring.version} first.`);
  }
  const same = active.find((r) => canonicalRule(r.structure) === canonicalRule(parsed.rule));
  if (same) {
    await c.audit("RULE_PROPOSED", "REFUSED", `same as v${same.version}`);
    return c.reply(`That is the same as active rule v${same.version} (arm B${same.version}), so it would only repeat that arm.`);
  }
  if (active.length >= MAX_ACTIVE_RULES) {
    await c.audit("RULE_PROPOSED", "REFUSED", "all slots in use");
    return c.reply(
      `${MAX_ACTIVE_RULES} rules are already active (${versionsOf(active)}). Stop one first with /retire <version>, then propose this again.`,
    );
  }
  const rule = await c.deps.store.proposeRule(parsed.rule, raw.trim().slice(0, MAX_RULE_TEXT_CHARS), c.updateId);
  await c.audit("RULE_PROPOSED", "OK", `v${rule.version}`);
  await c.reply(
    `Proposed rule v${rule.version} as arm B${rule.version}:\n${describeRule(rule.structure)}\n\n` +
      `Nothing changes until you reply YES (it waits ${PENDING_RULE_MINUTES} minutes). Reply NO to cancel. ` +
      `${active.length} of ${MAX_ACTIVE_RULES} rule slots are in use.`,
  );
};

const yes: Route = async (c) => {
  const store = c.deps.store;
  const pending = await store.pendingRule();
  if (pending) {
    const active = await store.activeRules();
    if (active.some((r) => canonicalRule(r.structure) === canonicalRule(pending.structure))) {
      await c.audit("RULE_ACTIVATED", "REFUSED", "repeats an active rule");
      return c.reply("That rule repeats one that is already active, so I did not start it. Reply NO to drop it.");
    }
    if (active.length >= MAX_ACTIVE_RULES) {
      await c.audit("RULE_ACTIVATED", "REFUSED", "all slots in use");
      return c.reply(`${MAX_ACTIVE_RULES} rules are already active (${versionsOf(active)}). Stop one with /retire <version> first.`);
    }
    await store.activateRule(pending, c.updateId);
    await c.audit("RULE_ACTIVATED", "OK", `v${pending.version}`);
    const all = [...active, pending];
    await c.reply(
      `Rule v${pending.version} is now active as arm B${pending.version}:\n${describeRule(pending.structure)}\n` +
        `Active rules: ${versionsOf(all)} (${all.length} of ${MAX_ACTIVE_RULES}). It applies to tickets frozen from now on` +
        (pending.structure.reverse?.basis === "odds_disagreement"
          ? ", but only to tickets from a screenshot with odds: the daily simulated ticket has none."
          : "."),
    );
    return;
  }
  const retiring = await store.pendingRetirement();
  if (retiring) {
    const active = await store.activeRules();
    await store.confirmRetirement(retiring, c.updateId);
    await c.audit("RULE_RETIRED", "OK", `v${retiring.version}`);
    const left = active.filter((r) => r.rule_id !== retiring.rule_id);
    await c.reply(
      `Rule v${retiring.version} is retired (arm B${retiring.version}). Its earlier picks stay and are still scored. ` +
        (left.length ? `Active rules: ${versionsOf(left)}.` : "No rule is active now, so tickets get arms A and C only."),
    );
    return;
  }
  await c.audit("RULE_ACTIVATED", "REFUSED", "nothing pending");
  await c.reply("Nothing is waiting for YES. Propose a rule with /rule or ask to stop one with /retire.");
};

const no: Route = async (c) => {
  const store = c.deps.store;
  const pending = await store.pendingRule();
  if (pending) {
    await store.declineRule(pending, c.updateId);
    await c.audit("RULE_DECLINED", "OK", `v${pending.version}`);
    return c.reply(`Cancelled rule v${pending.version}. The active rules are unchanged.`);
  }
  const retiring = await store.pendingRetirement();
  if (retiring) {
    await store.declineRetirement(retiring, c.updateId);
    await c.audit("RULE_DECLINED", "OK", `keep v${retiring.version}`);
    return c.reply(`Kept rule v${retiring.version} (arm B${retiring.version}). Nothing changed.`);
  }
  await c.audit("RULE_DECLINED", "REFUSED", "nothing pending");
  await c.reply("Nothing is waiting for a decision.");
};

const retire: Route = async (c) => {
  const store = c.deps.store;
  const [active, pending, retiring] = await Promise.all([store.activeRules(), store.pendingRule(), store.pendingRetirement()]);
  const list = active.length ? `Active rules: ${versionsOf(active)}.` : "No rule is active.";
  const m = (c.message.text ?? "").trim().match(/^\/retire(?:@\w+)?\s+v?(\d{1,6})$/i);
  if (!m) {
    await c.audit("RULE_RETIRE_REQUESTED", "REFUSED", "not understood");
    return c.reply(`Say which rule to stop, for example: /retire ${active[0]?.version ?? 3}. ${list}`);
  }
  const version = Number(m[1]);
  const rule = active.find((r) => r.version === version);
  if (!rule) {
    await c.audit("RULE_RETIRE_REQUESTED", "REFUSED", `v${version} not active`);
    return c.reply(`Rule v${version} is not active. ${list}`);
  }
  if (pending || retiring) {
    await c.audit("RULE_RETIRE_REQUESTED", "REFUSED", "something is waiting for YES");
    return c.reply("Answer YES or NO to what is already waiting first, then ask again.");
  }
  await store.requestRetirement(rule, c.updateId);
  await c.audit("RULE_RETIRE_REQUESTED", "OK", `v${version}`);
  await c.reply(
    `Stop rule v${version} (arm B${version}): ${describeRule(rule.structure)}?\n` +
      "It would no longer be used on tickets frozen from now on; its earlier picks stay and are still scored. " +
      `Reply YES to stop it (it waits ${PENDING_RULE_MINUTES} minutes) or NO to keep it.`,
  );
};

const rules: Route = async (c) => {
  const [active, pending, retiring] = await Promise.all([
    c.deps.store.activeRules(),
    c.deps.store.pendingRule(),
    c.deps.store.pendingRetirement(),
  ]);
  await c.audit("VIEW", "OK", "rules");
  const lines = active.length
    ? [
      `Active rules (${active.length} of ${MAX_ACTIVE_RULES}; each is its own arm B on the same fixtures as A and C):`,
      ...active.map((r) => `- B${r.version} (rule v${r.version}): ${describeRule(r.structure)}`),
    ]
    : ["No rule is active, so tickets get arms A and C only."];
  if (pending) lines.push(`Waiting for YES or NO: rule v${pending.version}: ${describeRule(pending.structure)}`);
  if (retiring) lines.push(`Waiting for YES or NO: stop rule v${retiring.version}`);
  await c.reply(lines.join("\n"));
};

const today: Route = async (c) => {
  const today = c.deps.now().toISOString().slice(0, 10);
  const tickets = await c.deps.store.ticketsForDate(today);
  await c.audit("VIEW", "OK", "today");
  if (tickets.length === 0) return c.reply(`No tickets for ${today} (UTC) yet.`);
  const out: string[] = [];
  for (const t of tickets) {
    out.push(`${t.kind === "SIMULATED" ? "Simulated" : "Screenshot"} ticket, ${t.fixtureCount} fixtures:`);
    const byFixture = new Map<string, { arm: string; text: string }[]>();
    for (const p of t.picks) {
      const key = `${p.home} v ${p.away}`;
      const mark = p.forced || p.adjustment === "FORCED_DRAW" ? " (forced)" : p.adjustment === "REVERSED" ? " (reversed)" : "";
      const arm = armLabel(p.arm, p.armVersion);
      byFixture.set(key, [...(byFixture.get(key) ?? []), { arm, text: `${arm} ${label(p.pick)}${mark}` }]);
    }
    for (const [fixture, picks] of byFixture) {
      out.push(`- ${fixture}: ${[...picks].sort((a, b) => compareArms(a.arm, b.arm)).map((x) => x.text).join(" | ")}`);
    }
  }
  await c.reply(out.join("\n"));
};

function scoreLines(kind: string, rows: readonly ScoreRow[]): string[] {
  const lines = [`${kind === "SIMULATED" ? "Simulated" : "Screenshot"} tickets:`];
  const ordered = [...rows].sort((a, b) => compareArms(armLabel(a.arm, a.arm_version), armLabel(b.arm, b.arm_version)));
  for (const r of ordered) {
    const name = r.arm === "A" ? "A (rating model)" : r.arm === "C" ? "C (draw-score-v1)" : `${armLabel(r.arm, r.arm_version)} (rule v${r.arm_version.replace(/^rule-v/, "")})`;
    lines.push(
      `- ${name}: ${r.picks_right} of ${r.picks_settled} right (${pct(r.picks_right, r.picks_settled)}, ${margin(r.picks_settled)}) over ${r.tickets} ticket(s)`,
    );
    if (r.arm !== "A") {
      lines.push(`  Same fixtures: A ${r.a_right_same_fixtures} right${r.arm === "B" ? `, C ${r.c_right_same_fixtures} right` : ""}`);
    }
    if (r.adjusted_settled > 0) {
      lines.push(
        `  Where it differs from A by design: ${r.adjusted_settled} settled, it was right ${r.adjusted_right} times and A ${r.a_right_on_adjusted}`,
      );
    } else if (r.arm !== "A") {
      lines.push("  It has not differed from A on a settled pick yet");
    }
    if (r.picks_settled < MIN_SETTLED_NOTE) lines.push(`  Only ${r.picks_settled} settled picks: too few to read much into`);
  }
  return lines;
}

const scoreboard: Route = async (c) => {
  const rows = await c.deps.store.scoreboard();
  await c.audit("VIEW", "OK", "scoreboard");
  if (rows.length === 0) return c.reply("No settled ticket picks yet.");
  const kinds = [...new Set(rows.map((r) => r.ticket_kind))].sort();
  // One message per ticket kind, so five rules never push a message past Telegram's length limit.
  for (const [i, kind] of kinds.entries()) {
    const lines = i === 0
      ? [
        "Hit rates on settled picks of complete tickets (a calibration report card, not evidence of an edge). " +
        "The ± is a rough 95% margin for one hit rate: a gap smaller than it is most likely noise.",
        "",
      ]
      : [];
    await c.reply([...lines, ...scoreLines(kind, rows.filter((r) => r.ticket_kind === kind))].join("\n"));
  }
};

/** /eli5: the performance message in plain words, from the scoreboard, the window summary, the ticket hits and the slate counts. */
const eli5: Route = async (c) => {
  const [score, windows] = await Promise.all([c.deps.store.scoreboard(), c.deps.store.windowSummary()]);
  // The slate line and the ticket check are bonuses: if their views cannot be read, the rest still goes out.
  let slate = null;
  let hits = null;
  let notes = "";
  try {
    slate = await c.deps.store.slateScore();
  } catch {
    notes += " (slate counts unavailable)";
  }
  try {
    hits = await c.deps.store.ticketHits();
  } catch {
    notes += " (ticket check unavailable)";
  }
  await c.audit("ELI5", "OK", `arms=${new Set(score.map((r) => r.arm_version)).size}${notes}`);
  await c.reply(buildEli5(score, windows, slate, hits, c.deps.now()));
};

interface FileRef {
  fileId: string;
  /** The declared media type; null for an upload kept as a file id only (the type is then read from the bytes). */
  mime: string | null;
  type: "IMAGE" | "PDF";
  size: number | null;
}

function fileOf(message: TgMessage): FileRef | "UNSUPPORTED" | null {
  if (message.photo && message.photo.length > 0) {
    const biggest = message.photo[message.photo.length - 1];
    return { fileId: biggest.file_id, mime: "image/jpeg", type: "IMAGE", size: biggest.file_size ?? null };
  }
  if (message.document) {
    const mime = message.document.mime_type ?? "";
    if (!ALLOWED_MEDIA.has(mime)) return "UNSUPPORTED";
    return {
      fileId: message.document.file_id,
      mime,
      type: mime === "application/pdf" ? "PDF" : "IMAGE",
      size: message.document.file_size ?? null,
    };
  }
  return null;
}

const tooLarge = `That file is larger than ${Math.round(MAX_FILE_BYTES / (1024 * 1024))} MB. Send a smaller one.`;

/** A file arrives: with ticket or slate in the caption it is read as that; without one the bot asks and reads nothing. */
const ticketFile: Route = async (c) => {
  const file = fileOf(c.message);
  if (file === null || file === "UNSUPPORTED") {
    await c.audit("BAD_INPUT", "REFUSED", "unsupported file");
    return c.reply("I can read a photo, a PNG, JPEG or WebP file, or a PDF. Send /help for the list.");
  }
  if (file.size !== null && file.size > MAX_FILE_BYTES) {
    await c.audit("BAD_INPUT", "REFUSED", "file too large");
    return c.reply(tooLarge);
  }
  const kind = captionKind(c.message.caption);
  if (kind !== null) return await readAndStore(c, file, kind, null);
  // No marker: keep only the Telegram file id (60 minutes, the latest upload only) and ask. Nothing is
  // downloaded, read or counted against today's limits until the answer.
  await c.deps.store.savePending({ updateId: c.updateId, fileId: file.fileId, fileType: file.type, fileSize: file.size });
  await c.audit("PENDING", "OK", `type=${file.type}`);
  await c.reply(
    "Is this a ticket or a slate?\n" +
      `- Ticket: a slip you declared, up to ${MAX_FIXTURES} rows. I match it, freeze arms A, B and C before kickoff and review it after the games.\n` +
      `- Slate: any size, up to ${SLATE_MAX_ROWS} rows. I match and store it for testing models; nothing is frozen and it stays off the scoreboard.\n` +
      `Reply /ticket or /slate within ${PENDING_UPLOAD_MINUTES} minutes. Only your latest upload is kept, and only its Telegram file id. ` +
      "Nothing has been read or counted against today's limits yet. Next time put the word ticket or slate in the caption and I will not ask.",
  );
};

/** /ticket and /slate: the answer to the question above, or a request to send the file first. */
const answerKind = (kind: ReadKind): Route => async (c) => {
  const pending = await c.deps.store.latestPending();
  // The database view already hides an expired upload; the clock is checked here too, so an expired one never gets read.
  if (pending === null || Date.parse(pending.expiresAt) <= c.deps.now().getTime()) {
    await c.audit("PENDING", "REFUSED", "nothing pending");
    return c.reply(
      "No upload is waiting (or the one I asked about expired after " + PENDING_UPLOAD_MINUTES + " minutes). " +
        `Send the photo, image file or PDF first, then answer /ticket or /slate, or put the word ${kind === "SLATE" ? "slate" : "ticket"} in its caption. ` +
        "Today's tickets are under /today.",
    );
  }
  await readAndStore(c, { fileId: pending.fileId, mime: null, type: pending.fileType, size: pending.fileSize }, kind, pending.id);
};

const REJECT_TEXT: Record<RejectReason, string> = {
  NOT_A_FIXTURE: "not a fixture",
  MISSING_TEAM: "a team name was missing or unreadable",
  SAME_TEAM: "the same team on both sides",
  OVER_LIMIT: `past the ${SLATE_MAX_ROWS}-row limit`,
};

function describeRejected(r: RejectedRow): string {
  const names = r.home !== null || r.away !== null ? `${r.home ?? "?"} v ${r.away ?? "?"}: ` : "";
  return `- row ${r.position}: ${names}${REJECT_TEXT[r.reason]}`;
}

/** Writes to the audit log without letting a logging failure stop the work it describes. */
async function softAudit(c: Ctx, action: AuditAction, outcome: AuditOutcome, detail: string): Promise<void> {
  try {
    await c.audit(action, outcome, detail);
  } catch {
    /* the audit write itself failed: nothing more to do */
  }
}

/** Reads one file as a ticket or a slate, matches it, stores it and replies. Shared by a caption marker and the answer command. */
async function readAndStore(c: Ctx, file: FileRef, kind: ReadKind, pendingUploadId: string | null): Promise<void> {
  const { deps, updateId } = c;
  const act: AuditAction = kind === "SLATE" ? "SLATE" : "TICKET";
  const today = deps.now().toISOString().slice(0, 10);

  const bytes = await deps.telegram.download(file.fileId, MAX_FILE_BYTES);
  if (bytes === null) {
    await c.audit("BAD_INPUT", "REFUSED", "file too large");
    return c.reply(tooLarge);
  }
  const sha = await sha256Hex(bytes);

  // The same bytes as an earlier read: use what was stored from it. No vision call, no cap, no tokens, so
  // this is looked at before the day's limits (a repeat of a file already read is allowed on a used-up day).
  let earlier: StoredRead | null = null;
  try {
    earlier = await deps.store.findRead(sha);
  } catch {
    await softAudit(c, "ERROR", "ERROR", "file hash lookup failed");
  }

  let parsed: ParsedTicket;
  let tokens: TokenUsage | null = null;
  if (earlier !== null) {
    parsed = { bookmaker: earlier.bookmaker, rowsRead: earlier.rowsRead, rows: earlier.rows, rejected: earlier.rejected };
    await c.audit("REUSED", "OK", `kind=${kind} rows=${earlier.rows.length}`);
  } else {
    // Today's limits, before anything is spent. If the usage look fails the call cap still holds through
    // reserveCall (atomic), so a failure here never blocks a read.
    let usage: DayUsage | null = null;
    try {
      usage = await deps.store.usageToday(today);
    } catch {
      await softAudit(c, "ERROR", "ERROR", "usage unreadable");
    }
    if (usage !== null && usage.tokens >= deps.dailyTokenBudget) {
      await c.audit("TOKEN_CAP", "REFUSED", `budget=${deps.dailyTokenBudget}`);
      return c.reply(
        `The daily reading budget of ${deps.dailyTokenBudget.toLocaleString("en-US")} tokens is used up, so I did not read this. It resets at 00:00 UTC.`,
      );
    }
    if (usage !== null && usage.calls >= deps.dailyCap) {
      await c.audit("CAP_REACHED", "REFUSED", `cap=${deps.dailyCap}`);
      return c.reply(`The daily limit of ${deps.dailyCap} screenshot reads is used up. It resets at 00:00 UTC.`);
    }
    if (!(await deps.store.reserveCall(today, deps.dailyCap))) {
      await c.audit("CAP_REACHED", "REFUSED", `cap=${deps.dailyCap}`);
      return c.reply(`The daily limit of ${deps.dailyCap} screenshot reads is used up. It resets at 00:00 UTC.`);
    }
    // A read can take a while: say so first, so the chat is not silent.
    await c.reply("Reading it now. This can take up to a minute.");
    try {
      const result = await deps.vision(
        buildVisionRequest({ model: deps.model, mediaType: file.mime ?? mediaTypeOf(file.type, bytes), base64: toBase64(bytes), today }),
      );
      tokens = result.usage;
      if (tokens !== null) {
        // The tokens were spent whether or not the answer turns out usable, so they count against the budget first.
        try {
          await deps.store.addTokens(today, tokens.input, tokens.output);
        } catch {
          await softAudit(c, "ERROR", "ERROR", "token total not saved");
        }
      }
      parsed = parseVisionOutput(result.output, { today, utcOffsetMinutes: deps.utcOffsetMinutes });
    } catch (error) {
      const code = error instanceof VisionError ? error.code : "ERROR";
      const reason = error instanceof VisionError && error.reason ? ` ${error.reason}` : "";
      await c.audit(act, "ERROR", `vision ${code}${reason}`.slice(0, 200));
      return c.reply(
        code === "BAD_RESPONSE" || code === "ERROR"
          ? "I could not read that file this time. Nothing was stored. Try again with a clearer image."
          : "The reading service did not accept or answer that request, so nothing was read or stored. This is not about the file's content; the cause is in the audit log.",
      );
    }
  }
  if (parsed.rows.length === 0) {
    await c.audit(act, "REFUSED", "no fixtures read");
    return c.reply("I could not find any fixtures in that file. Nothing was stored.");
  }
  if (kind === "TICKET" && parsed.rows.length > MAX_FIXTURES) {
    await c.audit(act, "REFUSED", `over ${MAX_FIXTURES} rows (${parsed.rows.length})`);
    return c.reply(
      `That file has ${parsed.rows.length} fixtures and a ticket holds at most ${MAX_FIXTURES}. I did not cut it and nothing was stored. ` +
        (pendingUploadId !== null
          ? `If it is a longer list for testing models, answer /slate (the upload is still waiting for ${PENDING_UPLOAD_MINUTES} minutes from when you sent it).`
          : "If it is a longer list for testing models, send it again with the word slate in the caption."),
    );
  }

  const kicks = parsed.rows.map((r) => (r.kickoff_at ? Date.parse(r.kickoff_at) : NaN)).filter((n) => !Number.isNaN(n));
  const stored = kicks.length > 0
    ? await deps.store.findFixtures(
      new Date(Math.min(...kicks) - MATCH_WINDOW_MS).toISOString(),
      new Date(Math.max(...kicks) + MATCH_WINDOW_MS + 1).toISOString(),
    )
    : [];
  // The matching memory: names that earlier tickets already tied to stored names, read together with the
  // built-in seed names. If the memory cannot be read, matching runs with the seed names alone this time.
  let aliases: Map<string, string> = seedAliases();
  try {
    aliases = buildAliases(await deps.store.matchedHistory());
  } catch {
    // no memory this time; leave a trace so a broken lookup is not silent
    await softAudit(c, "ERROR", "ERROR", "matching memory unreadable");
  }
  const matched: MatchedRow[] = matchRows(parsed.rows, stored, aliases, { anchor: deps.anchorMatching });
  const hits = matched.filter((m) => m.fixtureId !== null).length;
  const byName = matched.filter((m) => m.via === "LOOSE");
  const byOneTeam = matched.filter((m) => m.via === "ANCHOR");
  const remembered = matched.filter((m) => m.via === "ALIAS").length;
  const intakeId = await deps.store.saveIntake(
    {
      updateId,
      kind,
      inputTokens: tokens?.input ?? null,
      outputTokens: tokens?.output ?? null,
      pendingUploadId,
      fileSha256: sha,
      ticketDate: today,
      sourceKind: file.type === "PDF" ? "PDF" : "IMAGE",
      bookmaker: parsed.bookmaker,
      model: deps.model,
      rowsRead: parsed.rowsRead,
      rowsKept: parsed.rows.length,
      rowsMatched: hits,
    },
    matched,
    parsed.rejected,
  );
  await c.audit(
    act,
    "OK",
    `read=${parsed.rowsRead} kept=${parsed.rows.length} matched=${hits} remembered=${remembered} similar=${byName.length}` +
      (byOneTeam.length > 0 ? ` oneteam=${byOneTeam.length}` : ""),
  );

  // The reply: every row read, then the rows that were rejected with their reasons, then how the matching went.
  const lines = [
    `${kind === "SLATE" ? "Slate: read" : "Read"} ${parsed.rowsRead} row(s); using ${parsed.rows.length}. Matched ${hits}, unmatched ${matched.length - hits}.`,
  ];
  if (earlier !== null) {
    lines.push("I read this exact file before, so I reused that read: no new reading, and nothing counted against today's limits.");
  }
  lines.push(`Rows read (${matched.length}):`);
  for (const [i, m] of matched.entries()) lines.push(`${i + 1}. ${describeRow(m.row)}`);
  if (parsed.rejected.length > 0) {
    lines.push(`Rejected rows, not used (${parsed.rejected.length}):`);
    for (const r of parsed.rejected) lines.push(describeRejected(r));
  }
  if (remembered > 0) lines.push(`${remembered} matched through names I knew (remembered from earlier tickets, or on my built-in list).`);
  if (byName.length > 0) {
    lines.push("Matched by name similarity (check these; I will remember them for next time):");
    for (const m of byName) lines.push(`- ${describeRow(m.row)} = ${m.matchedTo}`);
  }
  if (byOneTeam.length > 0) {
    lines.push("Matched by one team and kickoff (check these; I will remember them for next time):");
    for (const m of byOneTeam) lines.push(`- ${describeRow(m.row)} = ${m.matchedTo}`);
  }
  const missing = matched.filter((m) => m.fixtureId === null);
  if (missing.length > 0) {
    lines.push("Not matched (not used):");
    for (const m of missing) lines.push(`- ${describeRow(m.row)} (${reasonText(m.reason)})`);
  }
  if (kind === "SLATE") {
    lines.push("Stored as a slate for testing models. Nothing is frozen, no picks are made, and it stays off the scoreboard.");
    return c.reply(lines.join("\n"));
  }
  if (hits === 0) {
    lines.push("Nothing to compute: no row matched a stored fixture.");
    return c.reply(lines.join("\n"));
  }
  const active = await deps.store.activeRules();
  const started = await deps.dispatch(intakeId);
  await c.audit("DISPATCH", started ? "OK" : "ERROR");
  lines.push(
    started
      ? `Computing arms A, C${
        active.length ? ` and ${active.map((r) => `B${r.version}`).join(", ")} (one per active rule)` : ""
      } now; the frozen picks follow in this chat.`
      : "I saved the read but could not start the computation. Nothing was frozen.",
  );
  await c.reply(lines.join("\n"));
}

function describeRow(row: ParsedRow): string {
  return `${row.home_team} v ${row.away_team}`;
}

function reasonText(reason: MatchedRow["reason"]): string {
  return reason === "NO_KICKOFF"
    ? "no readable kickoff time"
    : reason === "AMBIGUOUS"
    ? "more than one stored fixture fits"
    : "no stored fixture with these names and time";
}

/** Routing table: its keys must equal COMMANDS' names (a test enforces it, so /help cannot drift). */
export const ROUTES: Record<string, Route> = {
  ticket_file: ticketFile,
  rule: proposeRule,
  retire,
  yes,
  no,
  rules,
  ticket: answerKind("TICKET"),
  slate: answerKind("SLATE"),
  today,
  scoreboard,
  eli5,
  help,
};

export function commandFor(message: TgMessage): { name: string; kind: UpdateKind } {
  if (message.photo || message.document) {
    return { name: "ticket_file", kind: message.document?.mime_type === "application/pdf" ? "PDF" : "IMAGE" };
  }
  const t = (message.text ?? "").trim();
  if (/^yes[.!]?$/i.test(t)) return { name: "yes", kind: "COMMAND" };
  if (/^no[.!]?$/i.test(t)) return { name: "no", kind: "COMMAND" };
  const m = t.match(/^\/(\w+)(?:@\w+)?(?:\s|$)/);
  if (m) {
    const word = m[1].toLowerCase();
    const name = word === "start" ? "help" : word;
    if (COMMANDS.some((c) => c.name === name) && name !== "ticket_file" && name !== "yes" && name !== "no") {
      return { name, kind: "COMMAND" };
    }
  }
  return { name: "unknown", kind: "TEXT" };
}

export async function handleRequest(request: Request, deps: Deps): Promise<Response> {
  if (request.method !== "POST") return text("method not allowed", 405);
  const header = request.headers.get("x-telegram-bot-api-secret-token");
  if (header === null || !(await secretsEqual(header, deps.secretToken))) return text("unauthorized", 401);

  const length = Number(request.headers.get("content-length") ?? "0");
  if (length > MAX_BODY_BYTES) return text("payload too large", 413);
  const raw = await request.text();
  if (raw.length > MAX_BODY_BYTES) return text("payload too large", 413);
  let update: TgUpdate;
  try {
    update = JSON.parse(raw) as TgUpdate;
  } catch {
    return text("bad request", 400);
  }
  if (typeof update?.update_id !== "number") return text("bad request", 400);
  const message = update.message;
  if (!message || typeof message !== "object" || typeof message.chat?.id !== "number") return text("ok");

  const swallow = async (work: () => Promise<unknown>) => {
    try {
      await work();
    } catch {
      /* a logging or reply failure must not turn into a retry storm */
    }
  };

  // Only the operator's own private chat may do anything. A stranger gets no reply and a row with no content.
  const allowed = String(message.chat.id) === deps.allowedChatId && message.chat.type === "private" &&
    String(message.from?.id ?? "") === deps.allowedChatId;
  if (!allowed) {
    await swallow(() => deps.store.audit("REJECTED_CHAT", "REFUSED", null, update.update_id));
    return text("ok");
  }

  const chatId = String(message.chat.id);
  const { name, kind } = commandFor(message);
  const ctx: Ctx = {
    deps,
    updateId: update.update_id,
    chatId,
    message,
    // A reply longer than Telegram's limit goes out as several messages, in order, never cut.
    reply: async (body) => {
      for (const part of splitMessage(body)) await deps.telegram.send(chatId, part);
    },
    audit: (action, outcome, detail) => deps.store.audit(action, outcome, detail ?? null, update.update_id),
  };
  try {
    if (!(await deps.store.claimUpdate(update.update_id, kind))) return text("ok"); // a retried delivery
    const route = ROUTES[name];
    if (!route) {
      await ctx.reply("I did not understand that. Send /help for what I can do.");
    } else {
      await route(ctx);
    }
  } catch {
    await swallow(() => ctx.audit("ERROR", "ERROR", "unhandled"));
    await swallow(() => ctx.reply("Something went wrong and I could not finish. Nothing further was changed."));
  }
  await syncMenu(deps, (detail) => swallow(() => ctx.audit("ERROR", "ERROR", detail)));
  return text("ok");
}

// The command menu in Telegram is a second copy of the command list, so the function keeps it equal
// to MENU itself: checked after the operator's first message on a running instance and again at most
// once an hour (so a menu changed from outside is put back), after the reply has been sent (so it
// never delays one), and only for the operator's chat. After any deploy that changes a command, the
// menu follows with the next message. A failure is written to the audit log with a short reason,
// retried with the next message, and never changes what the operator sees.
export const MENU_RECHECK_MS = HOUR_MS;
const menuCheckedAt = new WeakMap<TelegramApi, number>();
async function syncMenu(deps: Deps, auditFailure: (detail: string) => Promise<unknown>): Promise<void> {
  const now = deps.now().getTime();
  const last = menuCheckedAt.get(deps.telegram);
  if (last !== undefined && now - last < MENU_RECHECK_MS) return;
  menuCheckedAt.set(deps.telegram, now);
  try {
    await deps.telegram.syncMenu(MENU);
  } catch (error) {
    menuCheckedAt.delete(deps.telegram);
    const reason = error instanceof TelegramError && error.reason ? ` ${error.reason}` : "";
    await auditFailure(`menu_sync${reason}`.slice(0, 200));
  }
}

export { HOUR_MS };
