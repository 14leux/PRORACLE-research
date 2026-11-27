import assert from "node:assert/strict";
import { PostgrestStore, StoreError } from "./store.ts";
import { TelegramError, TelegramHttp } from "./telegram.ts";

interface Seen {
  url: string;
  method: string;
  headers: Record<string, string>;
  body: unknown;
}

function recorder(...responses: (Response | Error)[]) {
  const seen: Seen[] = [];
  const fn = async (url: string, init?: RequestInit) => {
    const h: Record<string, string> = {};
    new Headers(init?.headers).forEach((v, k) => (h[k] = v));
    seen.push({ url, method: init?.method ?? "GET", headers: h, body: init?.body ? JSON.parse(String(init.body)) : undefined });
    const next = responses.shift() ?? new Response("[]");
    if (next instanceof Error) throw next;
    return next;
  };
  return { fn, seen };
}

const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status });
const KEY = "service-key-placeholder";

Deno.test("requests carry the key only in headers and select the proracle schema", async () => {
  const { fn, seen } = recorder(json([]), json([]));
  const store = new PostgrestStore("https://project.example", KEY, fn);
  await store.findFixtures("2026-11-26T09:00:00.000Z", "2026-11-26T15:00:00.000Z");
  await store.audit("HELP", "OK", "x", 5);
  for (const s of seen) {
    assert.ok(!s.url.includes(KEY));
    assert.equal(s.headers["apikey"], KEY);
    assert.equal(s.headers["authorization"], `Bearer ${KEY}`);
    assert.equal(s.headers["accept-profile"], "proracle");
  }
  assert.equal(seen[1].headers["content-profile"], "proracle");
  assert.equal(seen[0].headers["content-profile"], undefined);
});

Deno.test("fixtures are read for scheduled matches inside the window only", async () => {
  const { fn, seen } = recorder(json([{ id: "f1", home_team: "A", away_team: "B", kickoff_at: "x", competition_name: null }]));
  const found = await new PostgrestStore("https://p.example", KEY, fn).findFixtures("2026-11-26T09:00:00.000Z", "2026-11-26T15:00:00.000Z");
  assert.equal(found.length, 1);
  const url = new URL(seen[0].url);
  assert.equal(url.pathname, "/rest/v1/fixtures");
  assert.equal(url.searchParams.get("status"), "eq.SCHEDULED");
  assert.deepEqual(url.searchParams.getAll("kickoff_at"), ["gte.2026-11-26T09:00:00.000Z", "lt.2026-11-26T15:00:00.000Z"]);
});

Deno.test("a retried Telegram update is recognised through an insert that ignores duplicates", async () => {
  const { fn, seen } = recorder(json([{ update_id: 9, kind: "IMAGE" }]), json([]));
  const store = new PostgrestStore("https://p.example", KEY, fn);
  assert.equal(await store.claimUpdate(9, "IMAGE"), true);
  assert.equal(await store.claimUpdate(9, "IMAGE"), false);
  assert.equal(seen[0].headers["prefer"], "resolution=ignore-duplicates,return=representation");
  assert.equal(new URL(seen[0].url).searchParams.get("on_conflict"), "update_id");
  assert.deepEqual(seen[0].body, { update_id: 9, kind: "IMAGE" });
});

Deno.test("the daily call cap is taken through the database function", async () => {
  const { fn, seen } = recorder(json(true), json(false));
  const store = new PostgrestStore("https://p.example", KEY, fn);
  assert.equal(await store.reserveCall("2026-11-26", 10), true);
  assert.equal(await store.reserveCall("2026-11-26", 10), false);
  assert.equal(new URL(seen[0].url).pathname, "/rest/v1/rpc/intake_reserve_call");
  assert.deepEqual(seen[0].body, { p_date: "2026-11-26", p_cap: 10 });
});

Deno.test("a read is stored as parsed values only, row by row, with the matched fixture", async () => {
  const { fn, seen } = recorder(json([{ id: "intake-uuid" }]), json(null), json(null), json(null));
  const store = new PostgrestStore("https://p.example", KEY, fn);
  const id = await store.saveIntake(
    {
      updateId: 5, kind: "TICKET", inputTokens: 900, outputTokens: 80, pendingUploadId: null, fileSha256: "a".repeat(64),
      ticketDate: "2026-11-26", sourceKind: "PDF", bookmaker: "BookX", model: "m", rowsRead: 2, rowsKept: 2, rowsMatched: 1,
    },
    [
      { row: { home_team: "A", away_team: "B", kickoff_at: "2026-11-26T12:00:00.000Z", home_odds: 2, draw_odds: 3, away_odds: 4 }, fixtureId: "f1", reason: null, via: "EXACT" },
      { row: { home_team: "C", away_team: "D", kickoff_at: null, home_odds: null, draw_odds: null, away_odds: null }, fixtureId: null, reason: "NO_KICKOFF" },
    ],
    [{ position: 3, home: "X", away: "X", reason: "SAME_TEAM" }],
  );
  assert.equal(id, "intake-uuid");
  assert.deepEqual(Object.keys(seen[0].body as object).sort(), [
    "bookmaker_label", "input_tokens", "kind", "output_tokens", "pending_upload_id", "rows_kept", "rows_matched", "rows_read",
    "source_kind", "ticket_date", "update_id", "vision_model",
  ]);
  const rows = seen[1].body as Record<string, unknown>[];
  assert.deepEqual(rows.map((r) => r.position), [1, 2]);
  assert.deepEqual(rows.map((r) => r.matched_fixture_id), ["f1", null]);
  assert.deepEqual(Object.keys(rows[0]).sort(), [
    "away_odds", "away_team", "draw_odds", "home_odds", "home_team", "intake_id", "kickoff_at", "match_via", "matched_fixture_id",
    "position", "read_kind", "unmatched_reason",
  ]);
  assert.deepEqual(rows.map((r) => [r.match_via, r.unmatched_reason, r.read_kind]), [["EXACT", null, "TICKET"], [null, "NO_KICKOFF", "TICKET"]]);
  assert.deepEqual(seen[2].body, [{ intake_id: "intake-uuid", position: 3, home_team: "X", away_team: "X", reason: "SAME_TEAM" }]);
  assert.deepEqual(seen[3].body, { file_sha256: "a".repeat(64), intake_id: "intake-uuid" });
});

Deno.test("rule events are appended in order and an activation touches no other rule", async () => {
  const { fn, seen } = recorder(json([{ version: 4 }]), json([{ id: "rule-5" }]), json(null), json(null));
  const store = new PostgrestStore("https://p.example", KEY, fn);
  const rule = await store.proposeRule({ v: 1, force_draws: { count: 5, basis: "draw_score" } }, "5 draws", 12);
  assert.equal(rule.version, 5);
  assert.deepEqual(seen[2].body, { rule_id: "rule-5", event: "PROPOSED", update_id: 12 });
  await store.activateRule(rule, 13);
  assert.equal(seen.length, 4); // exactly one request: no earlier rule is retired
  assert.deepEqual(seen[3].body, { rule_id: "rule-5", event: "ACTIVATED", update_id: 13 });
});

Deno.test("retirement is asked, confirmed or declined through its own events, one request each", async () => {
  const { fn, seen } = recorder(json(null), json(null), json(null));
  const store = new PostgrestStore("https://p.example", KEY, fn);
  const rule = { rule_id: "rule-3", version: 3, structure: { v: 1 as const }, source_text: "x" };
  await store.requestRetirement(rule, 20);
  await store.confirmRetirement(rule, 21);
  await store.declineRetirement(rule, 22);
  assert.deepEqual(seen.map((s) => s.body), [
    { rule_id: "rule-3", event: "RETIRE_REQUESTED", update_id: 20 },
    { rule_id: "rule-3", event: "RETIRED", update_id: 21 },
    { rule_id: "rule-3", event: "RETIRE_DECLINED", update_id: 22 },
  ]);
  for (const s of seen) {
    assert.equal(s.method, "POST");
    assert.equal(new URL(s.url).pathname, "/rest/v1/intake_rule_events");
  }
});

Deno.test("every active rule is read from the plural view, oldest first, and the pending retirement from its own view", async () => {
  const rules = [{ rule_id: "a", version: 2, structure: { v: 1 }, source_text: "x" }, { rule_id: "b", version: 4, structure: { v: 1 }, source_text: "y" }];
  const { fn, seen } = recorder(json(rules), json([]), json([rules[1]]), json([]));
  const store = new PostgrestStore("https://p.example", KEY, fn);
  assert.deepEqual((await store.activeRules()).map((r) => r.version), [2, 4]);
  assert.deepEqual(await store.activeRules(), []);
  assert.equal((await store.pendingRetirement())?.version, 4);
  assert.equal(await store.pendingRetirement(), null);
  assert.equal(new URL(seen[0].url).pathname, "/rest/v1/intake_active_rules");
  assert.equal(new URL(seen[0].url).searchParams.get("order"), "version.asc");
  assert.equal(new URL(seen[2].url).pathname, "/rest/v1/intake_pending_retirement");
  assert.ok(seen.every((s) => s.method === "GET"));
});

Deno.test("the scoreboard is read from the per-rule view and ticket picks carry their arm version", async () => {
  const { fn, seen } = recorder(json([]), json([{ id: "t1", ticket_kind: "SIMULATED", fixture_count: 1 }]), json([{ ticket_id: "t1", fixture_id: "f1", arm: "B", arm_version: "rule-v3", pick: "DRAW", adjustment: "FORCED_DRAW", forced: false }]), json([{ id: "f1", home_team: "A", away_team: "B" }]));
  const store = new PostgrestStore("https://p.example", KEY, fn);
  await store.scoreboard();
  assert.equal(new URL(seen[0].url).pathname, "/rest/v1/intake_scoreboard");
  const [t] = await store.ticketsForDate("2026-11-26");
  assert.equal(t.picks[0].armVersion, "rule-v3");
  assert.ok(new URL(seen[2].url).searchParams.get("select")!.includes("arm_version"));
});

Deno.test("a failed request is a StoreError without the response body, URL or key", async () => {
  const { fn } = recorder(new Response("secret detail from the server", { status: 500 }), new Error("network down " + KEY));
  const store = new PostgrestStore("https://p.example", KEY, fn);
  for (let i = 0; i < 2; i++) {
    await assert.rejects(store.scoreboard(), (e) => {
      assert.ok(e instanceof StoreError);
      assert.ok(!String(e.message).includes("secret detail") && !String(e.message).includes(KEY));
      return true;
    });
  }
});

Deno.test("Telegram: replies are plain text, split rather than cut, and a file over the cap is not downloaded", async () => {
  const { fn, seen } = recorder(json({ ok: true }), json({ ok: true }), json({ ok: true, result: { file_path: "photos/x.jpg", file_size: 20_000_000 } }));
  const tg = new TelegramHttp("123:TOKEN-PLACEHOLDER", fn);
  await tg.send("42", "x".repeat(5000));
  const sent = seen[0].body as { chat_id: string; text: string; parse_mode?: string };
  const rest = seen[1].body as { text: string };
  assert.equal(sent.text.length, 4000);
  assert.equal(sent.text.length + rest.text.length, 5000, "the whole reply is delivered");
  assert.equal(sent.parse_mode, undefined); // no markup: nothing read from a screenshot can become formatting
  assert.equal(await tg.download("file-1", 10 * 1024 * 1024), null);
  assert.equal(seen.length, 3); // two message parts and getFile only: the oversized file was never fetched
});

Deno.test("Telegram menu sync: nothing is written when the menu already matches, the exact list is set when it differs", async () => {
  const menu = [{ command: "help", description: "Help me" }, { command: "rules", description: "Show rules" }];
  const same = recorder(json({ ok: true, result: menu }));
  assert.equal(await new TelegramHttp("t", same.fn).syncMenu(menu), "UNCHANGED");
  assert.deepEqual(same.seen.map((s) => s.url.split("/").pop()), ["getMyCommands"]);

  for (
    const stale of [
      [{ command: "help", description: "Help me" }], // a command is missing
      [{ command: "rules", description: "Show rules" }, { command: "help", description: "Help me" }], // order
      [{ command: "help", description: "Old text" }, { command: "rules", description: "Show rules" }], // wording
      [],
    ]
  ) {
    const differs = recorder(json({ ok: true, result: stale }), json({ ok: true, result: true }));
    assert.equal(await new TelegramHttp("t", differs.fn).syncMenu(menu), "UPDATED");
    assert.deepEqual(differs.seen.map((s) => s.url.split("/").pop()), ["getMyCommands", "setMyCommands"]);
    assert.deepEqual(differs.seen[1].body, { commands: menu });
  }

  // an unreadable current menu is replaced rather than trusted
  const unreadable = recorder(json({ ok: false }), json({ ok: true, result: true }));
  assert.equal(await new TelegramHttp("t", unreadable.fn).syncMenu(menu), "UPDATED");
});

Deno.test("the matching memory is read from the stored ticket rows with the matched fixture's names", async () => {
  const rows = [{ home_team: "HEARTS", away_team: "MONACO", matched: { home_team: "Heart of Midlothian", away_team: "AS Monaco" } }];
  const { fn, seen } = recorder(json(rows), json({ not: "an array" }));
  const store = new PostgrestStore("https://p.example", KEY, fn);
  assert.deepEqual(await store.matchedHistory(), rows);
  assert.deepEqual(await store.matchedHistory(), []);
  const url = new URL(seen[0].url);
  assert.equal(url.pathname, "/rest/v1/intake_ticket_rows");
  assert.equal(url.searchParams.get("select"), "home_team,away_team,matched:fixtures!matched_fixture_id(home_team,away_team)");
  assert.equal(url.searchParams.get("matched_fixture_id"), "not.is.null");
  assert.equal(url.searchParams.get("limit"), "2000");
  assert.equal(seen[0].method, "GET");
  assert.equal(seen[0].headers["accept-profile"], "proracle");
});

Deno.test("Telegram errors carry the HTTP status and Telegram's description as a reason, never the token", async () => {
  const notFound = recorder(json({ ok: false, error_code: 404, description: "Not Found" }, 404));
  await assert.rejects(
    () => new TelegramHttp("123:SECRET-TOKEN", notFound.fn).syncMenu([{ command: "help", description: "Help me" }]),
    (e: unknown) => e instanceof TelegramError && e.reason === "HTTP 404 Not Found" && !e.reason.includes("SECRET-TOKEN") && !e.message.includes("SECRET-TOKEN"),
  );
  const html = recorder(new Response("<html>", { status: 502 }));
  await assert.rejects(() => new TelegramHttp("t", html.fn).send("1", "x"), (e: unknown) => e instanceof TelegramError && e.reason === "HTTP 502");
  const down = recorder(new Error("boom https://api.telegram.org/bot123:SECRET-TOKEN/sendMessage"));
  await assert.rejects(
    () => new TelegramHttp("123:SECRET-TOKEN", down.fn).send("1", "x"),
    (e: unknown) => e instanceof TelegramError && e.reason === "network",
  );
});

Deno.test("Telegram menu sync: a refusal or a network error is a TelegramError that never carries the token", async () => {
  const menu = [{ command: "help", description: "Help me" }];
  const refused = recorder(json({ ok: true, result: [] }), json({ ok: false, description: "bad" }));
  await assert.rejects(() => new TelegramHttp("123:SECRET-TOKEN", refused.fn).syncMenu(menu), (e: Error) => !e.message.includes("SECRET-TOKEN"));
  const down = recorder(new Error("connect failed https://api.telegram.org/bot123:SECRET-TOKEN/getMyCommands"));
  await assert.rejects(() => new TelegramHttp("123:SECRET-TOKEN", down.fn).syncMenu(menu), (e: Error) => !e.message.includes("SECRET-TOKEN"));
});

Deno.test("Telegram: a download is returned in memory and a body larger than the cap is dropped", async () => {
  const small = recorder(json({ ok: true, result: { file_path: "docs/a.pdf", file_size: 3 } }), new Response(new Uint8Array([1, 2, 3])));
  assert.deepEqual(await new TelegramHttp("t", small.fn).download("f", 10), new Uint8Array([1, 2, 3]));
  assert.match(small.seen[1].url, /\/file\/bott\/docs\/a\.pdf$/);
  const big = recorder(json({ ok: true, result: { file_path: "docs/a.pdf" } }), new Response(new Uint8Array(20)));
  assert.equal(await new TelegramHttp("t", big.fn).download("f", 10), null);
});

Deno.test("a pending upload is stored as a file id only, and read back through the view that hides expired and used ones", async () => {
  const { fn, seen } = recorder(
    json(null),
    json([{ id: "p1", update_id: 77, file_id: "AgAD-file", file_type: "PDF", file_size: 4321, expires_at: "2026-11-26T10:00:00+00:00" }]),
    json([]),
  );
  const store = new PostgrestStore("https://p.example", KEY, fn);
  await store.savePending({ updateId: 77, fileId: "AgAD-file", fileType: "PDF", fileSize: 4321 });
  assert.equal(new URL(seen[0].url).pathname, "/rest/v1/intake_pending_uploads");
  assert.deepEqual(seen[0].body, { update_id: 77, file_id: "AgAD-file", file_type: "PDF", file_size: 4321 }, "no expiry sent: the database sets 60 minutes");
  const pending = await store.latestPending();
  assert.deepEqual(pending, { id: "p1", updateId: 77, fileId: "AgAD-file", fileType: "PDF", fileSize: 4321, expiresAt: "2026-11-26T10:00:00.000Z" });
  const url = new URL(seen[1].url);
  assert.equal(url.pathname, "/rest/v1/intake_pending_upload", "the view, not the table: only the newest, unexpired, unused row");
  assert.equal(url.searchParams.get("limit"), "1");
  assert.equal(await store.latestPending(), null);
});

Deno.test("a repeat file is found by its SHA-256 and its stored rows and rejected rows come back as the read", async () => {
  const sha = "b".repeat(64);
  const { fn, seen } = recorder(
    json([{ intake_id: "i-1" }]),
    json([{ bookmaker_label: "BookX", rows_read: 3 }]),
    json([{ home_team: "A", away_team: "B", kickoff_at: "2026-11-26T12:00:00+00:00", home_odds: 2, draw_odds: "3.1", away_odds: 4 }]),
    json([{ position: 3, home_team: "C", away_team: "C", reason: "SAME_TEAM" }]),
    json([]),
  );
  const store = new PostgrestStore("https://p.example", KEY, fn);
  const read = await store.findRead(sha);
  assert.deepEqual(read, {
    intakeId: "i-1",
    bookmaker: "BookX",
    rowsRead: 3,
    rows: [{ home_team: "A", away_team: "B", kickoff_at: "2026-11-26T12:00:00.000Z", home_odds: 2, draw_odds: 3.1, away_odds: 4 }],
    rejected: [{ position: 3, home: "C", away: "C", reason: "SAME_TEAM" }],
  });
  assert.equal(new URL(seen[0].url).searchParams.get("file_sha256"), `eq.${sha}`);
  assert.ok(seen.every((s) => !s.url.includes("file_id")), "only the hash is used, never the file");
  assert.equal(await store.findRead("c".repeat(64)), null, "an unknown hash is no earlier read");
});

Deno.test("today's usage is read from the usage row and one read's tokens are added through the database function", async () => {
  const { fn, seen } = recorder(
    json([{ vision_calls: 4, input_tokens: 90_000, output_tokens: 5_000 }]),
    json([]),
    json(null),
  );
  const store = new PostgrestStore("https://p.example", KEY, fn);
  assert.deepEqual(await store.usageToday("2026-11-26"), { calls: 4, tokens: 95_000 });
  assert.equal(new URL(seen[0].url).searchParams.get("usage_date"), "eq.2026-11-26");
  assert.deepEqual(await store.usageToday("2026-11-27"), { calls: 0, tokens: 0 }, "a day with no row has used nothing");
  await store.addTokens("2026-11-26", 1200, 300);
  assert.equal(new URL(seen[2].url).pathname, "/rest/v1/rpc/intake_add_tokens");
  assert.deepEqual(seen[2].body, { p_date: "2026-11-26", p_input: 1200, p_output: 300 });
});
