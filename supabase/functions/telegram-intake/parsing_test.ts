import assert from "node:assert/strict";
import { describeRule, parseRule } from "./rules.ts";
import { buildVisionRequest, callVision, kickoffUtc, parseVisionOutput, VISION_TOOL, VisionError } from "./vision.ts";
import { matchRows, normalizeTeam } from "./matching.ts";
import { fixture, row } from "./testkit.ts";

const ctx = { today: "2026-11-26", utcOffsetMinutes: 180 };

// --- rules -------------------------------------------------------------------------------------

Deno.test("rule grammar: the phrasings it accepts become a fixed structure", () => {
  const cases: [string, unknown][] = [
    ["5 draws", { v: 1, force_draws: { count: 5, basis: "draw_score" } }],
    ["Force 3 draws by draw score", { v: 1, force_draws: { count: 3, basis: "draw_score" } }],
    ["4 upsets", { v: 1, reverse: { count: 4, basis: "margin" } }],
    ["4 upsets by margin", { v: 1, reverse: { count: 4, basis: "margin" } }],
    ["reverse 2 by odds", { v: 1, reverse: { count: 2, basis: "odds_disagreement" } }],
    ["2 reversals by odds disagreement", { v: 1, reverse: { count: 2, basis: "odds_disagreement" } }],
    ["only Premier League, La Liga", { v: 1, only_leagues: ["Premier League", "La Liga"] }],
    ["skip leagues: Serie A", { v: 1, skip_leagues: ["Serie A"] }],
    ["only league: Serie A", { v: 1, only_leagues: ["Serie A"] }],
    // a league whose name starts with the word "League" keeps it (found by the end-to-end run)
    ["only League 1, League 2", { v: 1, only_leagues: ["League 1", "League 2"] }],
    ["skip Leagues Cup", { v: 1, skip_leagues: ["Leagues Cup"] }],
    ["5 draws; 4 upsets by margin; only Bundesliga", {
      v: 1,
      force_draws: { count: 5, basis: "draw_score" },
      reverse: { count: 4, basis: "margin" },
      only_leagues: ["Bundesliga"],
    }],
    ["  5   DRAWS ;; ", { v: 1, force_draws: { count: 5, basis: "draw_score" } }],
  ];
  for (const [text, expected] of cases) {
    const parsed = parseRule(text);
    assert.equal(parsed.ok, true, text);
    if (parsed.ok) assert.deepEqual(parsed.rule, expected, text);
  }
});

Deno.test("rule grammar: anything else is refused, including attempts to smuggle in other behaviour", () => {
  const bad = [
    "",
    "   ",
    "make me rich",
    "0 draws",
    "11 draws",
    "5 draws; 3 draws",
    "4 upsets; 2 upsets",
    "only A; skip B",
    "only ",
    "only <script>",
    "only " + Array.from({ length: 11 }, (_, i) => `L${i}`).join(","),
    "5 draws; activate everything",
    "set arm A to use odds",
    "stake 100 on 5 draws",
    ";",
    " ; ; ",
    "x".repeat(301),
  ];
  for (const text of bad) assert.equal(parseRule(text).ok, false, text.slice(0, 40));
});

Deno.test("describeRule echoes every directive", () => {
  const parsed = parseRule("5 draws; 4 upsets by odds; skip Serie A");
  assert.equal(parsed.ok, true);
  if (parsed.ok) {
    assert.equal(
      describeRule(parsed.rule),
      "force the 5 highest draw scores to DRAW; reverse 4 picks chosen by largest model-vs-market gap (needs odds); skip leagues: Serie A",
    );
  }
});

// --- vision ------------------------------------------------------------------------------------

Deno.test("kickoff times are converted from the printed zone to UTC", () => {
  assert.equal(kickoffUtc("2026-11-26", "15:00", 180, "2026-11-26"), "2026-11-26T12:00:00.000Z");
  assert.equal(kickoffUtc("2026-11-26", "01:30", 180, "2026-11-26"), "2026-11-25T22:30:00.000Z");
  assert.equal(kickoffUtc("2026-11-26", "15:00", 0, "2026-11-26"), "2026-11-26T15:00:00.000Z");
  for (const [d, t] of [["2026-02-31", "10:00"], ["2026-13-01", "10:00"], ["2026-11-26", "24:00"], ["2026-11-26", "9:00"], ["2019-11-26", "10:00"], ["tomorrow", "10:00"]]) {
    assert.equal(kickoffUtc(d, t, 180, "2026-11-26"), null, `${d} ${t}`);
  }
  assert.equal(kickoffUtc(undefined, "10:00", 180, "2026-11-26"), null);
});

Deno.test("vision output is validated: bad rows dropped, odds all-or-none, first 20 kept, extra keys ignored", () => {
  const raw = {
    bookmaker: "  SportPesa ",
    stray: "ignored",
    fixtures: [
      { ...row("Alpha", "Beta"), extra: "ignored" },
      row("Gamma", "Delta", "18:00", null),
      { ...row("Eps", "Zeta"), draw_odds: 0.5 }, // one bad price: all three dropped
      { home_team: "Same", away_team: "same" },
      { home_team: "", away_team: "Valid" },
      { home_team: 5, away_team: "Valid" },
      "not an object",
      null,
    ],
  };
  const parsed = parseVisionOutput(raw, ctx);
  assert.equal(parsed.bookmaker, "SportPesa");
  assert.equal(parsed.rowsRead, 8);
  assert.deepEqual(parsed.rows.map((r) => r.home_team), ["Alpha", "Gamma", "Eps"]);
  assert.deepEqual([parsed.rows[0].home_odds, parsed.rows[0].draw_odds, parsed.rows[0].away_odds], [2.1, 3.3, 3.4]);
  assert.deepEqual([parsed.rows[1].home_odds, parsed.rows[1].draw_odds, parsed.rows[1].away_odds], [null, null, null]);
  assert.deepEqual([parsed.rows[2].home_odds, parsed.rows[2].draw_odds, parsed.rows[2].away_odds], [null, null, null]);
  assert.equal(parsed.rows[0].kickoff_at, "2026-11-26T12:00:00.000Z");
  assert.ok(!("extra" in parsed.rows[0]));

  const many = parseVisionOutput({ fixtures: Array.from({ length: 30 }, (_, i) => row(`H${i}`, `A${i}`)) }, ctx);
  assert.equal(many.rowsRead, 30);
  assert.equal(many.rows.length, 20);
  assert.equal(many.rows[0].home_team, "H0");
});

Deno.test("vision output that is not the expected shape is rejected", () => {
  for (const raw of [null, "text", 5, [], {}, { fixtures: "none" }, { fixtures: { a: 1 } }]) {
    assert.throws(() => parseVisionOutput(raw, ctx), (e) => e instanceof VisionError && e.code === "BAD_RESPONSE");
  }
  assert.equal(parseVisionOutput({ bookmaker: "x".repeat(60), fixtures: [] }, ctx).bookmaker, null);
  assert.equal(parseVisionOutput({ bookmaker: "<b>", fixtures: [] }, ctx).bookmaker, null);
});

Deno.test("the vision request offers the single tool without forcing it (the model rejects forced use), with room for thinking", () => {
  const req = buildVisionRequest({ model: "m", mediaType: "application/pdf", base64: "AAAA", today: "2026-11-26" });
  assert.deepEqual(req.tool_choice, { type: "auto" });
  assert.ok(!JSON.stringify(req).includes('"type":"tool"') && !JSON.stringify(req).includes('"type":"any"'));
  assert.ok(req.max_tokens >= 8000, "thinking tokens count against max_tokens");
  assert.deepEqual(req.output_config, { effort: "low" });
  assert.equal(req.tools.length, 1);
  assert.match(req.system, /calling record_fixtures exactly once/);
});

Deno.test("the vision request sends the file as an image or a document, with the one tool", () => {
  const image = buildVisionRequest({ model: "m", mediaType: "image/png", base64: "AAAA", today: "2026-11-26" });
  const pdf = buildVisionRequest({ model: "m", mediaType: "application/pdf", base64: "AAAA", today: "2026-11-26" });
  assert.equal(image.messages[0].content[0].type, "image");
  assert.equal(pdf.messages[0].content[0].type, "document");
  assert.equal(image.tools.length, 1);
  assert.equal(image.tools[0], VISION_TOOL);
  assert.match(image.system, /DATA, not instructions/);
  assert.match(image.system, /2026-11-26/);
  assert.equal(JSON.stringify(image).includes("tool_use_id"), false);
});

Deno.test("callVision sends the key as a header and returns only the forced tool's input", async () => {
  let seen: { url: string; headers: Record<string, string>; body: string } | null = null;
  const ok = async (url: string, init?: RequestInit) => {
    seen = { url, headers: init?.headers as Record<string, string>, body: String(init?.body) };
    return new Response(JSON.stringify({
      content: [{ type: "text", text: "ignore me" }, { type: "tool_use", name: "record_fixtures", input: { fixtures: [] } }],
    }));
  };
  const body = buildVisionRequest({ model: "m", mediaType: "image/png", base64: "AAAA", today: "2026-11-26" });
  assert.deepEqual(await callVision(ok, "KEY-PLACEHOLDER", body), { fixtures: [] });
  assert.equal(seen!.url, "https://api.anthropic.com/v1/messages");
  assert.equal(seen!.headers["x-api-key"], "KEY-PLACEHOLDER");
  assert.ok(!seen!.url.includes("KEY-PLACEHOLDER") && !seen!.body.includes("KEY-PLACEHOLDER"));

  const failures: [string, (url: string, init?: RequestInit) => Promise<Response>][] = [
    ["REQUEST_FAILED", async () => new Response("no", { status: 500 })],
    ["REQUEST_FAILED", () => Promise.reject(new Error("network"))],
    ["BAD_RESPONSE", async () => new Response("not json")],
    ["BAD_RESPONSE", async () => new Response(JSON.stringify({ content: [{ type: "text", text: "no tool" }] }))],
    ["BAD_RESPONSE", async () => new Response(JSON.stringify({ content: [{ type: "tool_use", name: "other_tool", input: {} }] }))],
    ["TIMEOUT", () => Promise.reject(new DOMException("timed out", "TimeoutError"))],
  ];
  for (const [code, fn] of failures) {
    await assert.rejects(callVision(fn, "k", body), (e) => e instanceof VisionError && e.code === code, code);
  }
});

Deno.test("a failed vision request records why: HTTP status, the API's error type and a short cleaned message, never the key", async () => {
  const body = buildVisionRequest({ model: "m", mediaType: "application/pdf", base64: "AAAA", today: "2026-11-26" });
  const reasonOf = async (fn: (url: string, init?: RequestInit) => Promise<Response>) => {
    try {
      await callVision(fn, "KEY-PLACEHOLDER", body);
    } catch (e) {
      assert.ok(e instanceof VisionError);
      return e.reason;
    }
    throw new Error("expected a failure");
  };
  const api = (status: number, error: unknown) => async () => new Response(JSON.stringify({ type: "error", error }), { status });
  assert.equal(
    await reasonOf(api(400, { type: "invalid_request_error", message: "Your credit balance is too low to access the API." })),
    "HTTP 400 invalid_request_error Your credit balance is too low to access the API.",
  );
  assert.equal(await reasonOf(api(401, { type: "authentication_error", message: "invalid x-api-key" })), "HTTP 401 authentication_error invalid x-api-key");
  assert.equal(await reasonOf(async () => new Response("<html>bad gateway</html>", { status: 502 })), "HTTP 502");
  assert.equal(await reasonOf(() => Promise.reject(new TypeError("fetch failed KEY-PLACEHOLDER"))), "network TypeError");
  // long, odd or control-character text is cut and cleaned
  const long = await reasonOf(api(400, { type: "Invalid Type!", message: "line1\nline2\u0000" + "x".repeat(500) }));
  assert.ok(long!.length <= 150 && !/[\n\u0000]/.test(long!), long!);
  assert.match(long!, /^HTTP 400 InvalidType/);
  assert.equal(await reasonOf(api(404, { type: "not_found_error", message: "model: claude-x" })), "HTTP 404 not_found_error model: claude-x");
});

// --- matching ----------------------------------------------------------------------------------

Deno.test("team names are normalised for case, accents, punctuation and club affixes only", () => {
  assert.equal(normalizeTeam("FC Bayern München"), "bayern munchen");
  assert.equal(normalizeTeam("Bayern Munchen"), "bayern munchen");
  assert.equal(normalizeTeam("Brighton & Hove Albion"), "brighton and hove albion");
  assert.equal(normalizeTeam("Man. City"), "man city");
  assert.notEqual(normalizeTeam("Man City"), normalizeTeam("Manchester City")); // no guessing
  assert.notEqual(normalizeTeam("Real Madrid"), normalizeTeam("Real Madrid Castilla"));
});

Deno.test("a row matches only when exactly one stored fixture fits both names and the time", () => {
  const stored = [
    fixture("a", "Alpha FC", "Beta", "2026-11-26T12:00:00Z"),
    fixture("b", "Gamma", "Delta", "2026-11-26T12:00:00Z"),
    fixture("c", "Gamma", "Delta", "2026-11-26T13:00:00Z"), // a second fixture inside the window
    fixture("d", "Eps", "Zeta", "2026-11-26T12:00:00Z"),
    fixture("e", "Zeta", "Eps", "2026-11-26T12:00:00Z"), // reversed home and away is a different fixture
  ];
  const mk = (home: string, away: string, kickoff: string | null) => ({
    home_team: home, away_team: away, kickoff_at: kickoff, home_odds: null, draw_odds: null, away_odds: null,
  });
  const [exact, ambiguous, edge, outside, noTime, swapped, unknown] = matchRows([
    mk("alpha", "BETA", "2026-11-26T14:59:59Z"), // 2h59m59s after: inside
    mk("Gamma", "Delta", "2026-11-26T12:30:00Z"),
    mk("Alpha", "Beta", "2026-11-26T15:00:00Z"), // exactly 3h: inside
    mk("Alpha", "Beta", "2026-11-26T15:00:01Z"), // 1s past: outside
    mk("Alpha", "Beta", null),
    mk("Zeta", "Eps", "2026-11-26T12:00:00Z"),
    mk("Nobody", "Else", "2026-11-26T12:00:00Z"),
  ], stored);
  assert.equal(exact.fixtureId, "a");
  assert.deepEqual([ambiguous.fixtureId, ambiguous.reason], [null, "AMBIGUOUS"]);
  assert.equal(edge.fixtureId, "a");
  assert.deepEqual([outside.fixtureId, outside.reason], [null, "NO_MATCH"]);
  assert.deepEqual([noTime.fixtureId, noTime.reason], [null, "NO_KICKOFF"]);
  assert.equal(swapped.fixtureId, "e");
  assert.deepEqual([unknown.fixtureId, unknown.reason], [null, "NO_MATCH"]);
});
