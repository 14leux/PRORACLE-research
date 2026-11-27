import assert from "node:assert/strict";
import { describeRule, parseRule } from "./rules.ts";
import { buildVisionRequest, callVision, kickoffUtc, parseVisionOutput, VISION_TOOL, VisionError } from "./vision.ts";
import { buildAliases, looselySame, matchRows, normalizeTeam } from "./matching.ts";
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

Deno.test("vision output is validated: bad rows rejected with a reason, odds all-or-none, extra keys ignored", () => {
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
  assert.deepEqual(parsed.rejected, [
    { position: 4, home: "Same", away: "same", reason: "SAME_TEAM" },
    { position: 5, home: null, away: "Valid", reason: "MISSING_TEAM" },
    { position: 6, home: null, away: "Valid", reason: "MISSING_TEAM" },
    { position: 7, home: null, away: null, reason: "NOT_A_FIXTURE" },
    { position: 8, home: null, away: null, reason: "NOT_A_FIXTURE" },
  ]);
  assert.equal(parsed.bookmaker, "SportPesa");
  assert.equal(parsed.rowsRead, 8);
  assert.deepEqual(parsed.rows.map((r) => r.home_team), ["Alpha", "Gamma", "Eps"]);
  assert.deepEqual([parsed.rows[0].home_odds, parsed.rows[0].draw_odds, parsed.rows[0].away_odds], [2.1, 3.3, 3.4]);
  assert.deepEqual([parsed.rows[1].home_odds, parsed.rows[1].draw_odds, parsed.rows[1].away_odds], [null, null, null]);
  assert.deepEqual([parsed.rows[2].home_odds, parsed.rows[2].draw_odds, parsed.rows[2].away_odds], [null, null, null]);
  assert.equal(parsed.rows[0].kickoff_at, "2026-11-26T12:00:00.000Z");
  assert.ok(!("extra" in parsed.rows[0]));

  // A parse no longer cuts at 20: a ticket over 20 is refused by the handler, a slate keeps up to 100.
  const many = parseVisionOutput({ fixtures: Array.from({ length: 30 }, (_, i) => row(`H${i}`, `A${i}`)) }, ctx);
  assert.equal(many.rowsRead, 30);
  assert.equal(many.rows.length, 30);
  assert.equal(many.rows[0].home_team, "H0");
  assert.equal(many.rejected.length, 0);

  const huge = parseVisionOutput({ fixtures: Array.from({ length: 105 }, (_, i) => row(`H${i}`, `A${i}`)) }, ctx);
  assert.equal(huge.rows.length, 100);
  assert.deepEqual(huge.rejected.map((r) => [r.position, r.reason]), [[101, "OVER_LIMIT"], [102, "OVER_LIMIT"], [103, "OVER_LIMIT"], [104, "OVER_LIMIT"], [105, "OVER_LIMIT"]]);
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

Deno.test("normalisation also folds letters that accents do not decompose", () => {
  assert.equal(normalizeTeam("Jagiellonia Białystok"), normalizeTeam("JAGIELLONIA BIALYSTOK"));
  assert.equal(normalizeTeam("Bodø/Glimt"), normalizeTeam("Bodo Glimt"));
  assert.equal(normalizeTeam("Fenerbahçe"), "fenerbahce");
});

Deno.test("two names are loosely the same when one's words are all in the other, unless that changes the team", () => {
  const yes: [string, string][] = [
    ["Brighton", "Brighton & Hove Albion"],
    ["MARSEILLE", "Olympique de Marseille"],
    ["SL BENFICA", "Benfica"],
    ["SCU TORREENSE", "Torreense"],
    ["JAGIELLONIA", "Jagiellonia Białystok"],
    ["Inter", "Inter Milan"],
  ];
  for (const [a, b] of yes) {
    assert.ok(looselySame(normalizeTeam(a), normalizeTeam(b)), `${a} ~ ${b}`);
    assert.ok(looselySame(normalizeTeam(b), normalizeTeam(a)), `${b} ~ ${a}`);
  }
  const no: [string, string][] = [
    ["Manchester United", "Manchester United U21"], // youth side
    ["Arsenal", "Arsenal Women"],
    ["Barcelona", "Barcelona B"],
    ["Ajax", "Ajax II"],
    ["Real Madrid", "Real Madrid Castilla"],
    ["United", "Manchester United"], // only a generic word
    ["City", "Manchester City"],
    ["Man City", "Manchester City"], // different words, not contained
    ["RB Salzburg", "Red Bull Salzburg"],
    ["CS U Craiova", "Universitatea Craiova"],
    ["Juve", "Juventus"],
    ["Alpha", "Alpha"], // equal names are exact, not loose
    ["Atletico Madrid", "Real Madrid"],
  ];
  for (const [a, b] of no) assert.ok(!looselySame(normalizeTeam(a), normalizeTeam(b)), `${a} !~ ${b}`);
});

Deno.test("matching levels: exact beats remembered beats loose, ties are ambiguous, time and sides still bind", () => {
  const mk = (home: string, away: string, kickoff = "2026-11-26T12:00:00Z") => ({
    home_team: home, away_team: away, kickoff_at: kickoff, home_odds: null, draw_odds: null, away_odds: null,
  });
  const stored = [
    fixture("brighton", "Brighton & Hove Albion", "Universitatea Craiova", "2026-11-26T12:00:00Z"),
    fixture("hearts", "Heart of Midlothian", "AS Monaco", "2026-11-26T12:00:00Z"),
    fixture("real", "Real Madrid", "Getafe", "2026-11-26T12:00:00Z"),
    fixture("castilla", "Real Madrid Castilla", "Getafe", "2026-11-26T12:00:00Z"),
    fixture("a1", "Lazio", "Torino Calcio", "2026-11-26T12:00:00Z"),
    fixture("a2", "Lazio", "Torino Giovani", "2026-11-26T12:00:00Z"),
    fixture("t1", "Torino", "Genoa", "2026-11-26T12:00:00Z"),
    fixture("t2", "Torino Calcio", "Genoa", "2026-11-26T12:00:00Z"), // a loose candidate beside the exact one
  ];
  const aliases = new Map([["hearts", "heart of midlothian"], ["monaco", "as monaco"]]);
  const [loose, remembered, exactWins, ambiguous, late, swapped, none, exactBesideLoose] = matchRows([
    mk("BRIGHTON", "Universitatea Craiova"),
    mk("Hearts", "Monaco"),
    mk("Real Madrid", "Getafe"), // exact on "real" even though "castilla" would not be a loose match anyway
    mk("Lazio", "Torino"), // loosely fits both Torino fixtures
    mk("BRIGHTON", "Universitatea Craiova", "2026-11-26T15:00:01Z"), // just outside three hours
    mk("Universitatea Craiova", "BRIGHTON"), // home and away reversed
    mk("Brighton Rovers", "Universitatea Craiova"), // not contained: extra word on the screenshot side
    mk("Torino", "Genoa"), // an exact fixture and a loose one: the exact one wins
  ], stored, aliases);
  assert.deepEqual([exactBesideLoose.fixtureId, exactBesideLoose.via], ["t1", "EXACT"]);
  assert.deepEqual([loose.fixtureId, loose.via, loose.matchedTo], ["brighton", "LOOSE", "Brighton & Hove Albion v Universitatea Craiova"]);
  assert.deepEqual([remembered.fixtureId, remembered.via], ["hearts", "ALIAS"]);
  assert.deepEqual([exactWins.fixtureId, exactWins.via], ["real", "EXACT"]);
  assert.deepEqual([ambiguous.fixtureId, ambiguous.reason, ambiguous.via], [null, "AMBIGUOUS", null]);
  assert.deepEqual([late.fixtureId, late.reason], [null, "NO_MATCH"]);
  assert.deepEqual([swapped.fixtureId, swapped.reason], [null, "NO_MATCH"]);
  // CHANGED EXPECTATION (one-team match, DECISION 238): the away name fits exactly one fixture, so the home name
  // is taken as another spelling of that fixture's home team. The original "no match" still holds with the rule off.
  assert.deepEqual([none.fixtureId, none.via], ["brighton", "ANCHOR"]);
  assert.deepEqual(
    [matchRows([mk("Brighton Rovers", "Universitatea Craiova")], stored, aliases, { anchor: false })[0].fixtureId],
    [null],
  );
  // an alias pointing at a different team than the one stored is ignored (with the one-team rule off)
  const wrongTeam = [fixture("x", "Hibernian", "AS Monaco", "2026-11-26T12:00:00Z")];
  const [wrongAlias] = matchRows([mk("Hearts", "Monaco")], wrongTeam, aliases, { anchor: false });
  assert.equal(wrongAlias.fixtureId, null);
  // CHANGED EXPECTATION: with the rule on, "Monaco" still fits that fixture's away side, so it is matched by one team
  assert.deepEqual([matchRows([mk("Hearts", "Monaco")], wrongTeam, aliases)[0].fixtureId], ["x"]);
});

Deno.test("the matching memory keeps only names that always led to the same stored name", () => {
  const h = (home: string, away: string, matched: [string, string] | null) => ({
    home_team: home, away_team: away, matched: matched ? { home_team: matched[0], away_team: matched[1] } : null,
  });
  const aliases = buildAliases([
    h("HEARTS", "MONACO", ["Heart of Midlothian", "AS Monaco"]),
    h("Hearts", "Lyon", ["Heart of Midlothian", "Olympique Lyonnais"]), // same alias again: kept
    h("Dinamo", "Celtic", ["Dinamo Zagreb", "Celtic"]),
    h("Dinamo", "Kilmarnock", ["Dinamo Kyiv", "Kilmarnock"]), // "dinamo" now points two places: dropped
    h("Same", "Same", ["Same", "Same"]), // equal names need no alias
    h("Ghost", "Team", null), // an unmatched row teaches nothing
  ]);
  assert.equal(aliases.get("hearts"), "heart of midlothian");
  assert.equal(aliases.get("monaco"), "as monaco");
  assert.equal(aliases.get("lyon"), "olympique lyonnais");
  assert.equal(aliases.has("dinamo"), false);
  assert.equal(aliases.has("same"), false);
  assert.equal(aliases.has("ghost"), false);
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
