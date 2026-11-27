// Tickets and slates: the caption marker, the question when there is none, the pending upload, the
// 20-row refusal, the full reply, token logging and the budget, hash reuse, splitting and settings.
// Everything runs on fakes: no network, no clock, no secrets.

import assert from "node:assert/strict";
import { handleRequest } from "./handler.ts";
import { captionKind } from "./handler.ts";
import { COMMANDS, DEFAULT_TOKEN_BUDGET, helpText, MENU } from "./commands.ts";
import { intSetting } from "./settings.ts";
import { MAX_MESSAGE_CHARS, splitMessage } from "./telegram.ts";
import { fileMessage, fixture, makeKit, NOW, row, textMessage, webhook } from "./testkit.ts";

const KICKOFF = "2026-11-26T12:00:00Z";
const TODAY = "2026-11-26";

type Kit = ReturnType<typeof makeKit>;
const post = (kit: Kit, update: unknown) => handleRequest(webhook(update), kit.deps);
const many = (n: number, prefix = "Home") => Array.from({ length: n }, (_, i) => row(`${prefix} ${i + 1}`, `Away ${i + 1}`));
const stored = (kit: Kit, n: number, prefix = "Home") => {
  kit.store.fixtures = Array.from({ length: n }, (_, i) => fixture(`f${i + 1}`, `${prefix} ${i + 1}`, `Away ${i + 1}`, KICKOFF));
};
const sha256Hex = async (bytes: Uint8Array) =>
  [...new Uint8Array(await crypto.subtle.digest("SHA-256", bytes as BufferSource))].map((b) => b.toString(16).padStart(2, "0")).join("");

// --- (1) the caption marker, and asking when there is none --------------------------------------

Deno.test("captionKind: the word ticket or slate, alone, decides; both or neither means ask", () => {
  assert.equal(captionKind("ticket"), "TICKET");
  assert.equal(captionKind("Today's SLATE, please"), "SLATE");
  assert.equal(captionKind("/slate"), "SLATE");
  assert.equal(captionKind("ticket and slate"), null);
  assert.equal(captionKind("tickets"), null);
  assert.equal(captionKind("my picks"), null);
  assert.equal(captionKind(undefined), null);
});

Deno.test("a caption marker reads the file as that kind straight away, with no question", async () => {
  const t = makeKit();
  t.setVision({ fixtures: many(2) });
  stored(t, 2);
  await post(t, fileMessage("photo", { caption: "ticket" }));
  assert.equal(t.store.intakes[0].ticket.kind, "TICKET");
  assert.equal(t.store.pendings.length, 0);
  assert.deepEqual(t.dispatched, ["intake-1"]);

  const s = makeKit();
  s.setVision({ fixtures: many(2) });
  stored(s, 2);
  await post(s, fileMessage("photo", { caption: "slate" }));
  assert.equal(s.store.intakes[0].ticket.kind, "SLATE");
  assert.equal(s.store.pendings.length, 0);
  assert.equal(s.store.audits.find((a) => a.action === "SLATE")!.outcome, "OK");
});

Deno.test("with no marker the bot asks which it is and does nothing else: no download, no vision call, no cap spent", async () => {
  const kit = makeKit({ dailyCap: 1 });
  kit.setVision({ fixtures: many(2) });
  for (let i = 0; i < 3; i++) await post(kit, fileMessage("photo", { caption: null }));
  await post(kit, fileMessage({ mime: "application/pdf" }, { caption: "my picks" }));
  assert.equal(kit.visionCalls.length, 0);
  assert.equal(kit.tg.downloads.length, 0, "nothing is downloaded before the answer");
  assert.equal(kit.store.calls.size, 0, "asking spends none of the daily cap");
  assert.equal(kit.store.tokenTotals.size, 0);
  assert.equal(kit.store.intakes.length, 0);
  assert.equal(kit.store.pendings.length, 4);
  assert.match(kit.tg.sent[0].text, /Is this a ticket or a slate\?/);
  assert.match(kit.tg.sent[0].text, /Reply \/ticket or \/slate within 60 minutes/);
  assert.deepEqual(kit.store.audits.map((a) => [a.action, a.outcome]), Array(4).fill(["PENDING", "OK"]));
  // the daily cap of 1 is still whole: the answer takes it
  stored(kit, 2);
  await post(kit, textMessage("/slate"));
  assert.equal(kit.visionCalls.length, 1);
  assert.equal(kit.store.calls.get(TODAY), 1);
});

Deno.test("an upload is kept as its Telegram file id, type, size and update id only", async () => {
  const kit = makeKit();
  await post(kit, fileMessage("photo", { caption: null, updateId: 5001, size: 2345 }));
  await post(kit, fileMessage({ mime: "application/pdf", size: 7777 }, { caption: null, updateId: 5002 }));
  assert.deepEqual(
    kit.store.pendings.map((p) => ({ updateId: p.updateId, fileId: p.fileId, fileType: p.fileType, fileSize: p.fileSize })),
    [
      { updateId: 5001, fileId: "photo-big", fileType: "IMAGE", fileSize: 2345 },
      { updateId: 5002, fileId: "doc-1", fileType: "PDF", fileSize: 7777 },
    ],
  );
  assert.ok(!JSON.stringify(kit.store.pendings).includes("fixtures"), "no content, only the id");
});

Deno.test("the answer reads the waiting upload as that kind and uses it up", async () => {
  const kit = makeKit();
  kit.setVision({ fixtures: many(3) });
  stored(kit, 3);
  await post(kit, fileMessage("photo", { caption: null }));
  await post(kit, textMessage("/ticket"));
  const saved = kit.store.intakes[0];
  assert.equal(saved.ticket.kind, "TICKET");
  assert.equal(saved.ticket.pendingUploadId, "pending-1");
  assert.deepEqual(kit.tg.downloads, ["photo-big"]);
  assert.deepEqual(kit.dispatched, ["intake-1"]);
  // used once: a second answer finds nothing waiting
  await post(kit, textMessage("/slate"));
  assert.equal(kit.visionCalls.length, 1);
  assert.match(kit.tg.sent.at(-1)!.text, /No upload is waiting/);
});

Deno.test("only the latest upload is kept: the answer reads the newest and the older one is never read", async () => {
  const kit = makeKit();
  kit.setVision({ fixtures: many(2) });
  stored(kit, 2);
  await post(kit, fileMessage("photo", { caption: null }));
  await post(kit, fileMessage({ mime: "application/pdf" }, { caption: null }));
  await post(kit, textMessage("/slate"));
  assert.deepEqual(kit.tg.downloads, ["doc-1"]);
  assert.equal(kit.store.intakes[0].ticket.sourceKind, "PDF");
  assert.equal(kit.store.intakes[0].ticket.pendingUploadId, "pending-2");
  await post(kit, textMessage("/ticket"));
  assert.equal(kit.visionCalls.length, 1, "the older upload does not come back");
});

Deno.test("an upload expires after 60 minutes: the answer then asks for the file again and reads nothing", async () => {
  let clock = NOW;
  const kit = makeKit({ now: () => clock });
  kit.setVision({ fixtures: many(2) });
  stored(kit, 2);
  await post(kit, fileMessage("photo", { caption: null }));
  clock = new Date(NOW.getTime() + 59 * 60_000);
  assert.equal((await kit.store.latestPending())!.id, "pending-1");
  clock = new Date(NOW.getTime() + 61 * 60_000);
  await post(kit, textMessage("/ticket"));
  assert.equal(kit.visionCalls.length, 0);
  assert.equal(kit.tg.downloads.length, 0);
  assert.match(kit.tg.sent.at(-1)!.text, /No upload is waiting \(or the one I asked about expired after 60 minutes\)/);
  assert.deepEqual([kit.store.audits.at(-1)!.action, kit.store.audits.at(-1)!.outcome], ["PENDING", "REFUSED"]);
});

Deno.test("a bare /ticket or /slate with nothing pending says to send the file first", async () => {
  const kit = makeKit();
  for (const command of ["/ticket", "/slate", "/ticket@proracle_bot"]) {
    await post(kit, textMessage(command));
    const last = kit.tg.sent.at(-1)!.text;
    assert.match(last, /Send the photo, image file or PDF first/);
    assert.match(last, /\/today/);
  }
  assert.equal(kit.visionCalls.length, 0);
  assert.equal(kit.store.calls.size, 0);
});

Deno.test("the pending upload's file type tells the reader what it is: PDF bytes go as a document, PNG bytes as a png image", async () => {
  const png = makeKit();
  png.tg.files.set("doc-1", new Uint8Array([0x89, 0x50, 0x4e, 0x47, 1, 2, 3]));
  png.setVision({ fixtures: many(1) });
  await post(png, fileMessage({ mime: "image/png" }, { caption: null }));
  await post(png, textMessage("/slate"));
  const body = png.visionCalls[0] as { messages: { content: { type: string; source?: { media_type: string } }[] }[] };
  assert.equal(body.messages[0].content[0].type, "image");
  assert.equal(body.messages[0].content[0].source!.media_type, "image/png");

  const pdf = makeKit();
  pdf.setVision({ fixtures: many(1) });
  await post(pdf, fileMessage({ mime: "application/pdf" }, { caption: null }));
  await post(pdf, textMessage("/ticket"));
  const doc = pdf.visionCalls[0] as { messages: { content: { type: string; source?: { media_type: string } }[] }[] };
  assert.equal(doc.messages[0].content[0].type, "document");
  assert.equal(doc.messages[0].content[0].source!.media_type, "application/pdf");
});

// --- (2) /today replaces the old /ticket view, (6) help and menu for every command -----------------

Deno.test("/today shows today's tickets and /ticket is now the answer, not the view", async () => {
  const kit = makeKit();
  await post(kit, textMessage("/today"));
  assert.match(kit.tg.sent.at(-1)!.text, /No tickets for 2026-11-26/);
  assert.equal(kit.store.audits.at(-1)!.detail, "today");
  await post(kit, textMessage("/ticket"));
  assert.doesNotMatch(kit.tg.sent.at(-1)!.text, /No tickets for/);
});

Deno.test("every slash command has help text and a menu entry, and the new ones are listed", () => {
  const slash = COMMANDS.filter((c) => c.usage.startsWith("/"));
  for (const name of ["ticket", "slate", "today", "rule", "retire", "rules", "scoreboard", "help"]) {
    const c = slash.find((x) => x.name === name);
    assert.ok(c, name);
    assert.ok(c!.summary.length >= 10, `${name} has help text`);
    assert.ok(c!.menu !== undefined && c!.menu.length >= 3 && c!.menu.length <= 256, `${name} has menu text`);
    assert.ok(MENU.some((m) => m.command === name), `${name} is in the Telegram menu`);
  }
  const help = helpText(10);
  for (const word of ["/today", "/slate", "/ticket", "caption", "tokens", "60 minutes", "refused, never cut", "several messages"]) {
    assert.ok(help.includes(word), word);
  }
  assert.match(helpText(7, 123_456), /123,456 reading tokens per UTC day/);
});

// --- (3) a ticket over 20 rows is refused, never cut -------------------------------------------

Deno.test("a ticket over 20 rows is refused with a pointer to slate: nothing stored, nothing frozen, nothing cut", async () => {
  const kit = makeKit();
  kit.setVision({ fixtures: many(25) });
  stored(kit, 25);
  await post(kit, fileMessage("photo", { caption: "ticket" }));
  assert.equal(kit.store.intakes.length, 0);
  assert.deepEqual(kit.dispatched, []);
  const last = kit.tg.sent.at(-1)!.text;
  assert.match(last, /has 25 fixtures and a ticket holds at most 20/);
  assert.match(last, /slate/);
  assert.deepEqual([kit.store.audits.at(-1)!.action, kit.store.audits.at(-1)!.outcome, kit.store.audits.at(-1)!.detail], ["TICKET", "REFUSED", "over 20 rows (25)"]);
  assert.equal(kit.store.tokenTotals.get(TODAY), 1500, "the read that was refused still counts its tokens");
  // exactly 20 is still a ticket
  const edge = makeKit();
  edge.setVision({ fixtures: many(20) });
  stored(edge, 20);
  await post(edge, fileMessage("photo", { caption: "ticket" }));
  assert.equal(edge.store.intakes[0].rows.length, 20);
});

Deno.test("after the refusal the waiting upload can still be answered as a slate, and every row is kept", async () => {
  const kit = makeKit();
  kit.setVision({ fixtures: many(25) });
  stored(kit, 25);
  await post(kit, fileMessage("photo", { caption: null }));
  await post(kit, textMessage("/ticket"));
  assert.equal(kit.store.intakes.length, 0);
  assert.match(kit.tg.sent.at(-1)!.text, /answer \/slate/);
  await post(kit, textMessage("/slate"));
  const saved = kit.store.intakes[0];
  assert.equal(saved.ticket.kind, "SLATE");
  assert.equal(saved.rows.length, 25);
  assert.equal(saved.ticket.rowsKept, 25);
  assert.deepEqual(kit.dispatched, [], "a slate freezes nothing");
});

// --- slates: any size up to 100, no frozen picks ----------------------------------------------

Deno.test("a slate keeps up to 100 rows, lists the rest as rejected, freezes nothing and is not dispatched", async () => {
  const kit = makeKit();
  kit.setVision({ fixtures: many(105) });
  stored(kit, 100);
  await post(kit, fileMessage("photo", { caption: "slate" }));
  const saved = kit.store.intakes[0];
  assert.equal(saved.ticket.kind, "SLATE");
  assert.equal(saved.ticket.rowsRead, 105);
  assert.equal(saved.ticket.rowsKept, 100);
  assert.equal(saved.ticket.rowsMatched, 100);
  assert.equal(saved.rows.length, 100);
  assert.deepEqual(saved.rejected.map((r) => [r.position, r.reason]), [101, 102, 103, 104, 105].map((p) => [p, "OVER_LIMIT"]));
  assert.deepEqual(kit.dispatched, []);
  assert.ok(!kit.store.audits.some((a) => a.action === "DISPATCH"));
  assert.match(kit.tg.texts, /Stored as a slate for testing models/);
  assert.match(kit.tg.texts, /- row 105: Home 105 v Away 105: past the 100-row limit/);
});

// --- (4) the reply lists every row, the rejected rows with reasons, then the matching ---------------

Deno.test("the reply lists every row read, then rejected rows with reasons, then the matching result; nothing is cut at 10", async () => {
  const kit = makeKit();
  const fixtures = [...many(15), { home_team: "Same", away_team: "same" }, { home_team: "", away_team: "Valid" }, "junk"];
  kit.setVision({ fixtures });
  stored(kit, 3); // 3 of the 15 match, 12 do not
  await post(kit, fileMessage("photo", { caption: "ticket" }));
  const text = kit.tg.texts;
  for (let i = 1; i <= 15; i++) assert.ok(text.includes(`${i}. Home ${i} v Away ${i}`), `row ${i} is listed`);
  assert.match(text, /Read 18 row\(s\); using 15\. Matched 3, unmatched 12\./);
  assert.match(text, /Rejected rows, not used \(3\):/);
  assert.match(text, /- row 16: Same v same: the same team on both sides/);
  assert.match(text, /- row 17: \? v Valid: a team name was missing or unreadable/);
  assert.match(text, /- row 18: not a fixture/);
  for (let i = 4; i <= 15; i++) assert.ok(text.includes(`- Home ${i} v Away ${i} (no stored fixture with these names and time)`), `unmatched ${i}`);
  assert.doesNotMatch(text, /and \d+ more/);
  assert.ok(text.indexOf("Rows read (15):") < text.indexOf("Rejected rows") && text.indexOf("Rejected rows") < text.indexOf("Not matched (not used):"));
  assert.deepEqual(kit.store.intakes[0].rejected.map((r) => r.reason), ["SAME_TEAM", "MISSING_TEAM", "NOT_A_FIXTURE"]);
});

Deno.test("each stored row records how it matched, or why it did not", async () => {
  const kit = makeKit();
  kit.setVision({ fixtures: [row("Alpha", "Beta"), row("Brighton", "Universitatea Craiova"), row("Nobody", "Nowhere"), { ...row("Gamma", "Delta"), kickoff_date: "garbage" }] });
  kit.store.fixtures = [
    fixture("f1", "Alpha", "Beta", KICKOFF),
    fixture("f2", "Brighton & Hove Albion", "Universitatea Craiova", KICKOFF),
  ];
  await post(kit, fileMessage("photo", { caption: "slate" }));
  assert.deepEqual(
    kit.store.intakes[0].rows.map((m) => [m.fixtureId, m.via ?? null, m.reason]),
    [["f1", "EXACT", null], ["f2", "LOOSE", null], [null, null, "NO_MATCH"], [null, null, "NO_KICKOFF"]],
  );
});

// --- (5) tokens per read, the daily token budget, hash reuse -----------------------------------

Deno.test("input and output tokens are stored for every vision read and added to the day's total", async () => {
  const kit = makeKit();
  kit.setVision({ fixtures: many(2) });
  stored(kit, 2);
  kit.setUsage({ input: 4000, output: 900 });
  kit.tg.files.set("photo-big", new Uint8Array([1]));
  await post(kit, fileMessage("photo"));
  kit.setUsage({ input: 100, output: 20 });
  kit.tg.files.set("photo-big", new Uint8Array([2]));
  await post(kit, fileMessage("photo", { caption: "slate" }));
  assert.deepEqual(kit.store.intakes.map((i) => [i.ticket.inputTokens, i.ticket.outputTokens]), [[4000, 900], [100, 20]]);
  assert.equal(kit.store.tokenTotals.get(TODAY), 5020);
});

Deno.test("a response with no usage figures stores null tokens and adds nothing to the budget", async () => {
  const kit = makeKit();
  kit.setVision({ fixtures: many(1) });
  stored(kit, 1);
  kit.setUsage(null);
  await post(kit, fileMessage("photo"));
  assert.deepEqual([kit.store.intakes[0].ticket.inputTokens, kit.store.intakes[0].ticket.outputTokens], [null, null]);
  assert.equal(kit.store.tokenTotals.size, 0);
});

Deno.test("tokens of a read whose answer was unusable still count against the budget", async () => {
  const kit = makeKit();
  kit.setVision("not an object");
  await post(kit, fileMessage("photo"));
  assert.equal(kit.store.intakes.length, 0);
  assert.equal(kit.store.tokenTotals.get(TODAY), 1500);
});

Deno.test("a daily token budget refuses with a plain message before any read, and resets with the UTC day", async () => {
  let clock = NOW;
  const kit = makeKit({ dailyTokenBudget: 2000, now: () => clock });
  kit.setVision({ fixtures: many(1) });
  stored(kit, 1);
  kit.tg.files.set("photo-big", new Uint8Array([1]));
  await post(kit, fileMessage("photo")); // 1500 tokens: under the budget
  kit.tg.files.set("photo-big", new Uint8Array([2]));
  await post(kit, fileMessage("photo")); // 3000 in total now
  assert.equal(kit.visionCalls.length, 2, "the budget is checked before a read, so the one that crosses it still completes");
  kit.tg.files.set("photo-big", new Uint8Array([3]));
  await post(kit, fileMessage("photo"));
  assert.equal(kit.visionCalls.length, 2);
  assert.match(kit.tg.sent.at(-1)!.text, /daily reading budget of 2,000 tokens is used up.*resets at 00:00 UTC/);
  assert.deepEqual([kit.store.audits.at(-1)!.action, kit.store.audits.at(-1)!.outcome], ["TOKEN_CAP", "REFUSED"]);
  assert.equal(kit.store.calls.get(TODAY), 2, "a refused read takes no call from the cap");
  clock = new Date("2026-11-27T00:00:05Z");
  await post(kit, fileMessage("photo"));
  assert.equal(kit.visionCalls.length, 3, "a new UTC day starts with a fresh budget");
});

Deno.test("the budget also guards an upload read through the answer command", async () => {
  const kit = makeKit({ dailyTokenBudget: 1000 });
  kit.store.tokenTotals.set(TODAY, 1000);
  await post(kit, fileMessage("photo", { caption: null }));
  await post(kit, textMessage("/slate"));
  assert.equal(kit.visionCalls.length, 0);
  assert.match(kit.tg.sent.at(-1)!.text, /reading budget/);
});

Deno.test("if today's usage cannot be read the read still goes ahead (the call cap still holds) and the failure is audited", async () => {
  const kit = makeKit({ dailyCap: 1 });
  kit.setVision({ fixtures: many(1) });
  stored(kit, 1);
  kit.store.failOn = "usageToday";
  kit.tg.files.set("photo-big", new Uint8Array([1]));
  await post(kit, fileMessage("photo"));
  assert.equal(kit.store.intakes.length, 1);
  assert.ok(kit.store.audits.some((a) => a.action === "ERROR" && a.detail === "usage unreadable"));
  kit.tg.files.set("photo-big", new Uint8Array([2]));
  await post(kit, fileMessage("photo"));
  assert.equal(kit.visionCalls.length, 1, "the atomic call cap still refused the second read");
  assert.match(kit.tg.sent.at(-1)!.text, /daily limit of 1/);
});

Deno.test("a repeat upload of the same bytes reuses the earlier read: no vision call, no cap, no tokens", async () => {
  const kit = makeKit({ dailyCap: 1 });
  kit.setVision({ fixtures: [...many(3), { home_team: "X", away_team: "X" }] });
  stored(kit, 3);
  const bytes = new Uint8Array([7, 7, 7, 1]);
  kit.tg.files.set("photo-big", bytes);
  await post(kit, fileMessage("photo", { caption: "ticket" }));
  const first = kit.store.intakes[0];
  assert.equal(first.ticket.fileSha256, await sha256Hex(bytes));
  assert.equal(first.ticket.fileSha256!.length, 64);

  await post(kit, fileMessage("photo", { caption: "slate" }));
  assert.equal(kit.visionCalls.length, 1, "the same bytes are not read twice");
  assert.equal(kit.store.calls.get(TODAY), 1, "and take nothing from the cap");
  assert.equal(kit.store.tokenTotals.get(TODAY), 1500, "and add no tokens");
  const second = kit.store.intakes[1];
  assert.equal(second.ticket.kind, "SLATE", "the reused rows are stored as the kind just asked for");
  assert.deepEqual(second.rows.map((m) => m.row.home_team), ["Home 1", "Home 2", "Home 3"]);
  assert.equal(second.ticket.rowsRead, 4);
  assert.deepEqual(second.rejected.map((r) => r.reason), ["SAME_TEAM"]);
  assert.deepEqual([second.ticket.inputTokens, second.ticket.outputTokens], [null, null]);
  assert.equal(second.ticket.fileSha256, first.ticket.fileSha256);
  assert.ok(kit.store.audits.some((a) => a.action === "REUSED" && a.outcome === "OK"));
  assert.match(kit.tg.sent.at(-1)!.text, /I read this exact file before, so I reused that read/);
  assert.match(kit.tg.sent.at(-1)!.text, /Rejected rows, not used \(1\)/);

  // a file with other bytes is a new read
  kit.tg.files.set("photo-big", new Uint8Array([9]));
  await post(kit, fileMessage("photo", { caption: "slate" }));
  assert.equal(kit.visionCalls.length, 1, "this one is refused by the cap of 1: it was a new read");
  assert.match(kit.tg.sent.at(-1)!.text, /daily limit of 1/);
});

Deno.test("a reused read of more than 20 rows is refused as a ticket too, and a failed hash lookup just means a fresh read", async () => {
  const kit = makeKit();
  kit.setVision({ fixtures: many(30) });
  stored(kit, 30);
  kit.tg.files.set("photo-big", new Uint8Array([4]));
  await post(kit, fileMessage("photo", { caption: "slate" }));
  await post(kit, fileMessage("photo", { caption: "ticket" }));
  assert.equal(kit.visionCalls.length, 1);
  assert.equal(kit.store.intakes.length, 1);
  assert.match(kit.tg.sent.at(-1)!.text, /has 30 fixtures and a ticket holds at most 20/);

  const flaky = makeKit();
  flaky.setVision({ fixtures: many(1) });
  stored(flaky, 1);
  flaky.store.failOn = "findRead";
  await post(flaky, fileMessage("photo"));
  assert.equal(flaky.visionCalls.length, 1);
  assert.equal(flaky.store.intakes.length, 1);
  assert.ok(flaky.store.audits.some((a) => a.detail === "file hash lookup failed"));
});

// --- (7) long replies are split; a slow read says so first ---------------------------------------

Deno.test("splitMessage never cuts or drops text: parts fit, join back, and break at lines", () => {
  const lines = Array.from({ length: 300 }, (_, i) => `line ${i} ${"x".repeat(30)}`);
  const text = lines.join("\n");
  const parts = splitMessage(text, 1000);
  assert.ok(parts.length > 1);
  assert.ok(parts.every((p) => p.length <= 1000));
  assert.equal(parts.join("\n"), text);
  assert.deepEqual(splitMessage("short"), ["short"]);
  assert.deepEqual(splitMessage("a".repeat(MAX_MESSAGE_CHARS)), ["a".repeat(MAX_MESSAGE_CHARS)]);
  const long = splitMessage(("word ".repeat(2000)).trim(), 500);
  assert.ok(long.every((p) => p.length <= 500));
  assert.equal(long.join(" "), ("word ".repeat(2000)).trim());
  const unbroken = splitMessage("y".repeat(2500), 1000);
  assert.deepEqual(unbroken.map((p) => p.length), [1000, 1000, 500]);
});

Deno.test("a reply longer than Telegram's limit arrives as several messages with every row in them", async () => {
  const kit = makeKit();
  kit.setVision({ fixtures: many(100, "A very long club name indeed") });
  await post(kit, fileMessage("photo", { caption: "slate" }));
  const parts = kit.tg.sent.map((s) => s.text);
  assert.ok(parts.length >= 3, "the reading message, then the reply in at least two parts");
  assert.ok(parts.every((p) => p.length <= MAX_MESSAGE_CHARS));
  const all = parts.join("\n");
  for (const i of [1, 50, 100]) assert.ok(all.includes(`${i}. A very long club name indeed ${i} v Away ${i}`), `row ${i} arrived`);
  assert.ok(all.includes("Stored as a slate for testing models"), "the end of the reply arrived too");
});

Deno.test("a read says it is reading first; asking, refusals and reused reads do not need to", async () => {
  const kit = makeKit();
  kit.setVision({ fixtures: many(1) });
  stored(kit, 1);
  await post(kit, fileMessage("photo", { caption: "ticket" }));
  assert.match(kit.tg.sent[0].text, /^Reading it now/);
  assert.ok(kit.tg.sent.length >= 2, "the result follows");
  const asked = makeKit();
  await post(asked, fileMessage("photo", { caption: null }));
  assert.equal(asked.tg.sent.length, 1);
  assert.doesNotMatch(asked.tg.sent[0].text, /Reading it now/);
  const capped = makeKit({ dailyCap: 1 });
  capped.store.calls.set(TODAY, 1);
  await post(capped, fileMessage("photo"));
  assert.equal(capped.tg.sent.length, 1);
  assert.match(capped.tg.sent[0].text, /daily limit of 1/);
  const reuse = makeKit();
  reuse.setVision({ fixtures: many(1) });
  stored(reuse, 1);
  await post(reuse, fileMessage("photo"));
  const before = reuse.tg.sent.length;
  await post(reuse, fileMessage("photo"));
  assert.ok(!reuse.tg.sent.slice(before).some((s) => /Reading it now/.test(s.text)));
});

// --- (8) every setting has a safe default --------------------------------------------------------

Deno.test("a missing, empty, odd or out-of-range setting gives the default, never a refusal of everything", () => {
  assert.equal(intSetting(undefined, 7, 1, 100), 7);
  assert.equal(intSetting("", 7, 1, 100), 7);
  assert.equal(intSetting("   ", 7, 1, 100), 7);
  assert.equal(intSetting("abc", 7, 1, 100), 7);
  assert.equal(intSetting("4.5", 7, 1, 100), 7);
  assert.equal(intSetting("0", 7, 1, 100), 7, "0 never means a cap of zero");
  assert.equal(intSetting("-3", 7, 1, 100), 7);
  assert.equal(intSetting("101", 7, 1, 100), 7);
  assert.equal(intSetting("20", 7, 1, 100), 20);
  assert.ok(DEFAULT_TOKEN_BUDGET >= 1_000_000, "the default budget is far above a normal day");
});

Deno.test("with the default settings a normal day of reads never hits the budget", async () => {
  const kit = makeKit();
  kit.setVision({ fixtures: many(1) });
  stored(kit, 1);
  kit.setUsage({ input: 60_000, output: 12_000 }); // a long PDF read
  for (let i = 0; i < 10; i++) {
    kit.tg.files.set("photo-big", new Uint8Array([i]));
    await post(kit, fileMessage("photo"));
  }
  assert.equal(kit.visionCalls.length, 10);
  assert.ok(!kit.store.audits.some((a) => a.action === "TOKEN_CAP"));
});
