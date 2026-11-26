// The webhook handler. Order of checks: method, secret header (constant time), body size, the sender
// (one private chat only), retry deduplication, then the command. Every dependency is injected, so
// the tests run with fakes and no network.

import { ALLOWED_MEDIA, buildVisionRequest, parseVisionOutput, type ParsedRow, type ParsedTicket, VisionError } from "./vision.ts";
import { COMMANDS, helpText, MAX_FILE_BYTES, MAX_FIXTURES, MAX_RULE_TEXT_CHARS, MENU, PENDING_RULE_MINUTES } from "./commands.ts";
import { buildAliases, MATCH_WINDOW_MS, matchRows, type MatchedRow } from "./matching.ts";
import { describeRule, parseRule } from "./rules.ts";
import type { AuditAction, AuditOutcome, IntakeStore, UpdateKind } from "./store.ts";
import { TelegramError, type TelegramApi } from "./telegram.ts";

export interface Deps {
  secretToken: string;
  allowedChatId: string;
  dailyCap: number;
  model: string;
  utcOffsetMinutes: number;
  store: IntakeStore;
  telegram: TelegramApi;
  /** One vision call: request body in, the forced tool's input out. */
  vision: (body: ReturnType<typeof buildVisionRequest>) => Promise<unknown>;
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

const LABEL = { HOME_WIN: "home", DRAW: "draw", AWAY_WIN: "away" } as Record<string, string>;
const label = (pick: string) => LABEL[pick] ?? pick;

function pct(n: number, d: number): string {
  return d === 0 ? "n/a" : `${((100 * n) / d).toFixed(1)}%`;
}

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
  await c.reply(helpText(c.deps.dailyCap));
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
  const rule = await c.deps.store.proposeRule(parsed.rule, raw.trim().slice(0, MAX_RULE_TEXT_CHARS), c.updateId);
  await c.audit("RULE_PROPOSED", "OK", `v${rule.version}`);
  await c.reply(
    `Proposed rule v${rule.version} for arm B:\n${describeRule(rule.structure)}\n\n` +
      `Nothing changes until you reply YES (it waits ${PENDING_RULE_MINUTES} minutes). Reply NO to cancel.`,
  );
};

const yes: Route = async (c) => {
  const pending = await c.deps.store.pendingRule();
  if (!pending) {
    await c.audit("RULE_ACTIVATED", "REFUSED", "nothing pending");
    return c.reply("Nothing is waiting for YES. Propose one with /rule.");
  }
  const previous = await c.deps.store.activeRule();
  await c.deps.store.activateRule(pending, previous, c.updateId);
  await c.audit("RULE_ACTIVATED", "OK", `v${pending.version}`);
  await c.reply(
    `Rule v${pending.version} is now active for arm B:\n${describeRule(pending.structure)}` +
      (previous ? `\n(v${previous.version} is retired.)` : "") +
      "\nIt applies to tickets frozen from now on.",
  );
};

const no: Route = async (c) => {
  const pending = await c.deps.store.pendingRule();
  if (!pending) {
    await c.audit("RULE_DECLINED", "REFUSED", "nothing pending");
    return c.reply("Nothing is waiting for a decision.");
  }
  await c.deps.store.declineRule(pending, c.updateId);
  await c.audit("RULE_DECLINED", "OK", `v${pending.version}`);
  await c.reply(`Cancelled rule v${pending.version}. The active rule is unchanged.`);
};

const rules: Route = async (c) => {
  const [active, pending] = await Promise.all([c.deps.store.activeRule(), c.deps.store.pendingRule()]);
  await c.audit("VIEW", "OK", "rules");
  const lines = [
    active
      ? `Active rule v${active.version} (arm B): ${describeRule(active.structure)}`
      : "No rule is active, so tickets get arms A and C only.",
  ];
  if (pending) lines.push(`Waiting for YES or NO: rule v${pending.version}: ${describeRule(pending.structure)}`);
  await c.reply(lines.join("\n"));
};

const ticket: Route = async (c) => {
  const today = c.deps.now().toISOString().slice(0, 10);
  const tickets = await c.deps.store.ticketsForDate(today);
  await c.audit("VIEW", "OK", "ticket");
  if (tickets.length === 0) return c.reply(`No tickets for ${today} (UTC) yet.`);
  const out: string[] = [];
  for (const t of tickets) {
    out.push(`${t.kind === "SIMULATED" ? "Simulated" : "Screenshot"} ticket, ${t.fixtureCount} fixtures:`);
    const byFixture = new Map<string, string[]>();
    for (const p of t.picks) {
      const key = `${p.home} v ${p.away}`;
      const mark = p.forced || p.adjustment === "FORCED_DRAW" ? " (forced)" : p.adjustment === "REVERSED" ? " (reversed)" : "";
      byFixture.set(key, [...(byFixture.get(key) ?? []), `${p.arm} ${label(p.pick)}${mark}`]);
    }
    // Each cell starts with its arm letter, so a plain sort puts A, B, C in order.
    for (const [fixture, picks] of byFixture) out.push(`- ${fixture}: ${[...picks].sort().join(" | ")}`);
  }
  await c.reply(out.join("\n"));
};

const scoreboard: Route = async (c) => {
  const rows = await c.deps.store.scoreboard();
  await c.audit("VIEW", "OK", "scoreboard");
  if (rows.length === 0) return c.reply("No settled ticket picks yet.");
  const lines = ["Arm hit rates on settled picks of complete tickets (a calibration report card, not evidence of an edge):"];
  for (const r of rows) {
    lines.push(
      `- ${r.ticket_kind === "SIMULATED" ? "Simulated" : "Screenshot"} tickets, arm ${r.arm}: ` +
        `${r.picks_right} of ${r.picks_settled} right (${pct(r.picks_right, r.picks_settled)}) over ${r.tickets} ticket(s)`,
    );
  }
  await c.reply(lines.join("\n"));
};

function fileOf(message: TgMessage): { fileId: string; mime: string; size: number | null } | "UNSUPPORTED" | null {
  if (message.photo && message.photo.length > 0) {
    const biggest = message.photo[message.photo.length - 1];
    return { fileId: biggest.file_id, mime: "image/jpeg", size: biggest.file_size ?? null };
  }
  if (message.document) {
    const mime = message.document.mime_type ?? "";
    if (!ALLOWED_MEDIA.has(mime)) return "UNSUPPORTED";
    return { fileId: message.document.file_id, mime, size: message.document.file_size ?? null };
  }
  return null;
}

const ticketFile: Route = async (c) => {
  const { deps, updateId } = c;
  const file = fileOf(c.message);
  if (file === null || file === "UNSUPPORTED") {
    await c.audit("BAD_INPUT", "REFUSED", "unsupported file");
    return c.reply("I can read a photo, a PNG, JPEG or WebP file, or a PDF. Send /help for the list.");
  }
  if (file.size !== null && file.size > MAX_FILE_BYTES) {
    await c.audit("BAD_INPUT", "REFUSED", "file too large");
    return c.reply(`That file is larger than ${Math.round(MAX_FILE_BYTES / (1024 * 1024))} MB. Send a smaller one.`);
  }
  const now = deps.now();
  const today = now.toISOString().slice(0, 10);
  if (!(await deps.store.reserveCall(today, deps.dailyCap))) {
    await c.audit("CAP_REACHED", "REFUSED", `cap=${deps.dailyCap}`);
    return c.reply(`The daily limit of ${deps.dailyCap} screenshot reads is used up. It resets at 00:00 UTC.`);
  }
  const bytes = await deps.telegram.download(file.fileId, MAX_FILE_BYTES);
  if (bytes === null) {
    await c.audit("BAD_INPUT", "REFUSED", "file too large");
    return c.reply(`That file is larger than ${Math.round(MAX_FILE_BYTES / (1024 * 1024))} MB. Send a smaller one.`);
  }

  let parsed: ParsedTicket;
  try {
    const raw = await deps.vision(buildVisionRequest({ model: deps.model, mediaType: file.mime, base64: toBase64(bytes), today }));
    parsed = parseVisionOutput(raw, { today, utcOffsetMinutes: deps.utcOffsetMinutes });
  } catch (error) {
    const code = error instanceof VisionError ? error.code : "ERROR";
    const reason = error instanceof VisionError && error.reason ? ` ${error.reason}` : "";
    await c.audit("TICKET", "ERROR", `vision ${code}${reason}`.slice(0, 200));
    return c.reply(
      code === "BAD_RESPONSE" || code === "ERROR"
        ? "I could not read that file this time. Nothing was stored. Try again with a clearer image."
        : "The reading service did not accept or answer that request, so nothing was read or stored. This is not about the file's content; the cause is in the audit log.",
    );
  }
  if (parsed.rows.length === 0) {
    await c.audit("TICKET", "REFUSED", "no fixtures read");
    return c.reply("I could not find any fixtures in that file. Nothing was stored.");
  }

  const kicks = parsed.rows.map((r) => (r.kickoff_at ? Date.parse(r.kickoff_at) : NaN)).filter((n) => !Number.isNaN(n));
  const stored = kicks.length > 0
    ? await deps.store.findFixtures(
      new Date(Math.min(...kicks) - MATCH_WINDOW_MS).toISOString(),
      new Date(Math.max(...kicks) + MATCH_WINDOW_MS + 1).toISOString(),
    )
    : [];
  // The matching memory: names that earlier tickets already tied to stored names. If it cannot be read,
  // matching simply runs without it this time.
  let aliases: Map<string, string> = new Map();
  try {
    aliases = buildAliases(await deps.store.matchedHistory());
  } catch {
    // no memory this time; leave a trace so a broken lookup is not silent
    try {
      await c.audit("ERROR", "ERROR", "matching memory unreadable");
    } catch {
      /* the audit write itself failed: nothing more to do */
    }
  }
  const matched: MatchedRow[] = matchRows(parsed.rows, stored, aliases);
  const hits = matched.filter((m) => m.fixtureId !== null).length;
  const byName = matched.filter((m) => m.via === "LOOSE");
  const remembered = matched.filter((m) => m.via === "ALIAS").length;
  const intakeId = await deps.store.saveIntake(
    {
      updateId,
      ticketDate: today,
      sourceKind: file.mime === "application/pdf" ? "PDF" : "IMAGE",
      bookmaker: parsed.bookmaker,
      model: deps.model,
      rowsRead: parsed.rowsRead,
      rowsKept: parsed.rows.length,
      rowsMatched: hits,
    },
    matched,
  );
  await c.audit(
    "TICKET",
    "OK",
    `read=${parsed.rowsRead} kept=${parsed.rows.length} matched=${hits} remembered=${remembered} similar=${byName.length}`,
  );

  const lines = [`Read ${parsed.rowsRead} row(s); using ${parsed.rows.length}. Matched ${hits}, unmatched ${matched.length - hits}.`];
  if (parsed.rowsRead > MAX_FIXTURES) lines.push(`Only the first ${MAX_FIXTURES} valid rows are used.`);
  if (remembered > 0) lines.push(`${remembered} matched through names I remembered from earlier tickets.`);
  if (byName.length > 0) {
    lines.push("Matched by name similarity (check these; I will remember them for next time):");
    for (const m of byName.slice(0, 10)) lines.push(`- ${describeRow(m.row)} = ${m.matchedTo}`);
    if (byName.length > 10) lines.push(`- and ${byName.length - 10} more`);
  }
  const missing = matched.filter((m) => m.fixtureId === null);
  if (missing.length > 0) {
    lines.push("Not matched (not used):");
    for (const m of missing.slice(0, 10)) lines.push(`- ${describeRow(m.row)} (${reasonText(m.reason)})`);
    if (missing.length > 10) lines.push(`- and ${missing.length - 10} more`);
  }
  if (hits === 0) {
    lines.push("Nothing to compute: no row matched a stored fixture.");
    return c.reply(lines.join("\n"));
  }
  const rule = await deps.store.activeRule();
  const started = await deps.dispatch(intakeId);
  await c.audit("DISPATCH", started ? "OK" : "ERROR");
  lines.push(
    started
      ? `Computing arms A, C${rule ? ` and B (rule v${rule.version})` : ""} now; the frozen picks follow in this chat.`
      : "I saved the read but could not start the computation. Nothing was frozen.",
  );
  await c.reply(lines.join("\n"));
};

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
  yes,
  no,
  rules,
  ticket,
  scoreboard,
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
    reply: (body) => deps.telegram.send(chatId, body),
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
