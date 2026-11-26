import assert from "node:assert/strict";
import { PostgrestStore, StoreError } from "./store.ts";
import { TelegramHttp } from "./telegram.ts";

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
  const { fn, seen } = recorder(json([{ id: "intake-uuid" }]), json(null));
  const store = new PostgrestStore("https://p.example", KEY, fn);
  const id = await store.saveIntake(
    { updateId: 5, ticketDate: "2026-11-26", sourceKind: "PDF", bookmaker: "BookX", model: "m", rowsRead: 2, rowsKept: 2, rowsMatched: 1 },
    [
      { row: { home_team: "A", away_team: "B", kickoff_at: "2026-11-26T12:00:00.000Z", home_odds: 2, draw_odds: 3, away_odds: 4 }, fixtureId: "f1", reason: null },
      { row: { home_team: "C", away_team: "D", kickoff_at: null, home_odds: null, draw_odds: null, away_odds: null }, fixtureId: null, reason: "NO_KICKOFF" },
    ],
  );
  assert.equal(id, "intake-uuid");
  assert.deepEqual(Object.keys(seen[0].body as object).sort(), [
    "bookmaker_label", "rows_kept", "rows_matched", "rows_read", "source_kind", "ticket_date", "update_id", "vision_model",
  ]);
  const rows = seen[1].body as Record<string, unknown>[];
  assert.deepEqual(rows.map((r) => r.position), [1, 2]);
  assert.deepEqual(rows.map((r) => r.matched_fixture_id), ["f1", null]);
  assert.deepEqual(Object.keys(rows[0]).sort(), [
    "away_odds", "away_team", "draw_odds", "home_odds", "home_team", "intake_id", "kickoff_at", "matched_fixture_id", "position",
  ]);
});

Deno.test("rule events are appended in order and a new activation retires the old rule", async () => {
  const { fn, seen } = recorder(json([{ version: 4 }]), json([{ id: "rule-5" }]), json(null), json(null), json(null));
  const store = new PostgrestStore("https://p.example", KEY, fn);
  const rule = await store.proposeRule({ v: 1, force_draws: { count: 5, basis: "draw_score" } }, "5 draws", 12);
  assert.equal(rule.version, 5);
  assert.deepEqual(seen[2].body, { rule_id: "rule-5", event: "PROPOSED", update_id: 12 });
  await store.activateRule(rule, { rule_id: "rule-4", version: 4, structure: { v: 1 }, source_text: "" }, 13);
  assert.deepEqual(seen[3].body, { rule_id: "rule-5", event: "ACTIVATED", update_id: 13 });
  assert.deepEqual(seen[4].body, { rule_id: "rule-4", event: "RETIRED", update_id: 13 });
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

Deno.test("Telegram: replies are plain text, clipped, and a file over the cap is not downloaded", async () => {
  const { fn, seen } = recorder(json({ ok: true }), json({ ok: true, result: { file_path: "photos/x.jpg", file_size: 20_000_000 } }));
  const tg = new TelegramHttp("123:TOKEN-PLACEHOLDER", fn);
  await tg.send("42", "x".repeat(5000));
  const sent = seen[0].body as { chat_id: string; text: string; parse_mode?: string };
  assert.equal(sent.text.length, 4000);
  assert.equal(sent.parse_mode, undefined); // no markup: nothing read from a screenshot can become formatting
  assert.equal(await tg.download("file-1", 10 * 1024 * 1024), null);
  assert.equal(seen.length, 2); // getFile only: the oversized file was never fetched
});

Deno.test("Telegram: a download is returned in memory and a body larger than the cap is dropped", async () => {
  const small = recorder(json({ ok: true, result: { file_path: "docs/a.pdf", file_size: 3 } }), new Response(new Uint8Array([1, 2, 3])));
  assert.deepEqual(await new TelegramHttp("t", small.fn).download("f", 10), new Uint8Array([1, 2, 3]));
  assert.match(small.seen[1].url, /\/file\/bott\/docs\/a\.pdf$/);
  const big = recorder(json({ ok: true, result: { file_path: "docs/a.pdf" } }), new Response(new Uint8Array(20)));
  assert.equal(await new TelegramHttp("t", big.fn).download("f", 10), null);
});
