// /eli5: the plain-language performance message. Verdicts (too early, working, dropping), the 20-pick floor,
// the margin of error, the wording (never stakes, profit or advice), the slate line, and the command's wiring.
// Everything runs on fakes: no network, no clock, no secrets.

import assert from "node:assert/strict";
import { handleRequest } from "./handler.ts";
import { COMMANDS, helpText, MENU } from "./commands.ts";
import { buildEli5, ELI5_CLOSING, judge, marginPoints, MIN_SETTLED_FOR_VERDICT, slateLine } from "./eli5.ts";
import type { RecentRow, ScoreRow, SlateScoreRow } from "./store.ts";
import { makeKit, textMessage, webhook } from "./testkit.ts";
import { PostgrestStore } from "./store.ts";

type Kit = ReturnType<typeof makeKit>;
const post = (kit: Kit, update: unknown) => handleRequest(webhook(update), kit.deps);

const recent = (over: Partial<RecentRow>): RecentRow => ({
  ticket_kind: "SIMULATED", arm: "A", arm_version: "elo_davidson_recalibrated", window_name: "RECENT",
  picks_settled: 40, picks_right: 22, a_right_same_fixtures: 22, ...over,
});
const score = (over: Partial<ScoreRow>): ScoreRow => ({
  ticket_kind: "SIMULATED", arm: "A", arm_version: "elo_davidson_recalibrated", tickets: 3, picks_settled: 90, picks_right: 45,
  a_right_same_fixtures: 45, c_right_same_fixtures: 40, adjusted_settled: 0, adjusted_right: 0, a_right_on_adjusted: 0, ...over,
});
const slate = (over: Partial<SlateScoreRow>): SlateScoreRow => ({
  slate_reads: 2, fixtures: 40, settled: 30, model_hits: 14, compared: 10, compared_model_hits: 5, compared_market_hits: 6, ...over,
});

// words that would give advice, stakes or money talk; none may appear anywhere in the message
const ADVICE = /\b(stakes?|bankroll|profits?|winnings?|losses|you should|should you|bet on|back (?:it|them)|recommend\w*|guarantee\w*|sure thing|tip)\b/i;

// --- verdicts -------------------------------------------------------------------------------------

Deno.test("fewer than 20 settled picks is always too early to tell, however big the gap", () => {
  for (const n of [0, 1, 19]) {
    const v = judge({ picks_settled: n, picks_right: n, a_right_same_fixtures: 0 });
    assert.equal(v.kind, "too_early", `n=${n}`);
    assert.equal(v.kind === "too_early" && v.reason, "few_picks");
  }
  assert.equal(judge(undefined).kind, "too_early");
  assert.equal(MIN_SETTLED_FOR_VERDICT, 20);
});

Deno.test("with 20 settled picks, a gap inside the margin of error is too early to tell", () => {
  // n = 20: margin is round(98 / sqrt(20)) = 22 points, so 4 more hits than A (20 points) is inside it
  const v = judge({ picks_settled: 20, picks_right: 14, a_right_same_fixtures: 10 });
  assert.equal(v.kind, "too_early");
  assert.equal(v.kind === "too_early" && v.reason, "inside_margin");
});

Deno.test("working: at least 20 settled picks and ahead of A by more than the margin", () => {
  // n = 100: margin is 10 points; 12 more hits than A is 12 points
  const v = judge({ picks_settled: 100, picks_right: 62, a_right_same_fixtures: 50 });
  assert.equal(v.kind, "working");
  assert.equal(v.kind === "working" && v.margin, 10);
});

Deno.test("dropping: at least 20 settled picks and behind A by more than the margin", () => {
  const v = judge({ picks_settled: 100, picks_right: 38, a_right_same_fixtures: 50 });
  assert.equal(v.kind, "dropping");
});

Deno.test("behind A by less than the margin is not dropping, however many picks", () => {
  // n = 100, margin 10 points: 5 fewer hits than A is a gap of -5, inside the margin
  const v = judge({ picks_settled: 100, picks_right: 45, a_right_same_fixtures: 50 });
  assert.equal(v.kind, "too_early");
  assert.equal(v.kind === "too_early" && v.reason, "inside_margin");
  assert.equal(judge({ picks_settled: 100, picks_right: 40, a_right_same_fixtures: 50 }).kind, "too_early");
  assert.equal(judge({ picks_settled: 100, picks_right: 39, a_right_same_fixtures: 50 }).kind, "dropping");
});

Deno.test("a gap exactly at the margin is not a verdict (it has to be larger)", () => {
  // n = 100, margin 10 points: exactly 10 more hits than A is a gap of 10.0, not larger than the margin
  assert.equal(judge({ picks_settled: 100, picks_right: 60, a_right_same_fixtures: 50 }).kind, "too_early");
  assert.equal(judge({ picks_settled: 100, picks_right: 61, a_right_same_fixtures: 50 }).kind, "working");
});

Deno.test("the margin is the scoreboard's own margin of error", async () => {
  const kit = makeKit();
  kit.store.score = [score({ picks_settled: 100 })];
  await post(kit, textMessage("/scoreboard"));
  assert.match(kit.tg.sent.at(-1)!.text, new RegExp(`±${marginPoints(100)} pts`));
  assert.equal(marginPoints(100), 10);
  assert.equal(marginPoints(25), 20);
});

// --- the message ----------------------------------------------------------------------------------

Deno.test("the message calls each non-A arm working, dropping or too early, and states why", () => {
  const text = buildEli5(
    [
      score({}),
      score({ arm: "B", arm_version: "rule-v1", picks_settled: 100, picks_right: 60, a_right_same_fixtures: 48 }),
      score({ arm: "C", arm_version: "draw-score-v1", picks_settled: 100, picks_right: 40, a_right_same_fixtures: 52 }),
      score({ arm: "B", arm_version: "rule-v2", picks_settled: 8, picks_right: 3, a_right_same_fixtures: 3 }),
    ],
    [
      recent({ picks_settled: 100, picks_right: 52, a_right_same_fixtures: 52 }),
      recent({ arm: "B", arm_version: "rule-v1", picks_settled: 100, picks_right: 64, a_right_same_fixtures: 52 }),
      recent({ arm: "C", arm_version: "draw-score-v1", picks_settled: 100, picks_right: 38, a_right_same_fixtures: 52 }),
      recent({ arm: "B", arm_version: "rule-v2", picks_settled: 8, picks_right: 3, a_right_same_fixtures: 3 }),
    ],
    null,
  );
  assert.match(text, /B1, your rule v1: right 64 of 100[^\n]*\n {2}Working: it is ahead of A by 12\.0 points/);
  assert.match(text, /C, the draw-heavy picker: right 38 of 100[^\n]*\n {2}Dropping: it is behind A by 14\.0 points/);
  assert.match(text, /B2, your rule v2: right 3 of 8[^\n]*\n {2}Too early to tell: only 8 settled picks, and I wait for 20\./);
  // arm A is the yardstick: it never gets a verdict
  const lineA = text.split("\n").find((l) => l.startsWith("- A, the rating model"))!;
  assert.ok(lineA.includes("right 52 of 100"));
  assert.doesNotMatch(text.split("\n").slice(text.split("\n").indexOf(lineA), text.split("\n").indexOf(lineA) + 2).join("\n"), /Working|Dropping|Too early/);
  assert.match(text, /Since the start: right 45 of 90/);
});

Deno.test("an arm with nothing settled in the last 28 days is too early to tell, not working or dropping", () => {
  const text = buildEli5(
    [score({ arm: "B", arm_version: "rule-v3", picks_settled: 100, picks_right: 70, a_right_same_fixtures: 40 })],
    [],
    null,
  );
  assert.match(text, /B3, your rule v3: nothing settled in the last 28 days\.\n {2}Too early to tell: only 0 settled picks/);
  assert.doesNotMatch(text, /Working:|Dropping:/);
});

Deno.test("the 28 days before are shown as context and never decide the verdict", () => {
  const text = buildEli5(
    [],
    [
      recent({ arm: "C", arm_version: "draw-score-v1", picks_settled: 30, picks_right: 12, a_right_same_fixtures: 12 }),
      recent({ arm: "C", arm_version: "draw-score-v1", window_name: "PRIOR", picks_settled: 100, picks_right: 80, a_right_same_fixtures: 10 }),
    ],
    null,
  );
  assert.match(text, /The 28 days before: right 80 of 100 \(80%\)/);
  assert.match(text, /Too early to tell: its gap from A/);
});

Deno.test("with nothing settled at all it says it is too early to tell about anything", () => {
  const text = buildEli5([], [], null);
  assert.match(text, /Nothing has settled yet, so it is too early to tell about anything/);
  assert.doesNotMatch(text, /Working:|Dropping:/);
});

Deno.test("the message never talks about stakes, profit, advice or what to back, in any state", () => {
  const states: [ScoreRow[], RecentRow[], SlateScoreRow | null][] = [
    [[], [], null],
    [[score({})], [recent({})], slate({})],
    [
      [score({ arm: "B", arm_version: "rule-v1" }), score({ arm: "C", arm_version: "draw-score-v1" })],
      [
        recent({ arm: "B", arm_version: "rule-v1", picks_settled: 100, picks_right: 70, a_right_same_fixtures: 40 }),
        recent({ arm: "C", arm_version: "draw-score-v1", picks_settled: 100, picks_right: 30, a_right_same_fixtures: 60 }),
      ],
      slate({ compared: 0 }),
    ],
  ];
  for (const [s, r, sl] of states) {
    const text = buildEli5(s, r, sl);
    assert.doesNotMatch(text, ADVICE);
    assert.ok(text.endsWith(ELI5_CLOSING));
    assert.match(text, /not betting advice/);
  }
});

Deno.test("the command's own help text also keeps clear of stakes and profit", () => {
  const spec = COMMANDS.find((c) => c.name === "eli5")!;
  assert.match(spec.summary, /never about stakes or profit/);
  assert.match(spec.summary, /20 settled picks/);
  assert.doesNotMatch(spec.menu!, ADVICE);
});

// --- the slate line -------------------------------------------------------------------------------

Deno.test("the slate line appears only once slates have settled, and uses counts only", () => {
  assert.equal(slateLine(null), null);
  assert.equal(slateLine(slate({ settled: 0 })), null);
  const line = slateLine(slate({}))!;
  assert.match(line, /^Slates: for the 30 settled matches on them \(each counted once\), the rating model's pick was right 14 times \(47%\)\./);
  assert.match(line, /Where all three prices were printed \(10\), the market's favourite was right 6 times and the rating model 5\./);
  assert.doesNotMatch(slateLine(slate({ compared: 0 }))!, /prices/);
  assert.match(slateLine(slate({ settled: 1, model_hits: 1, compared: 0 }))!, /for the 1 settled match on them/);
  // in the whole message it comes before the closing line, as one line
  const text = buildEli5([], [recent({})], slate({}));
  assert.equal(text.split("\n").filter((l) => l.startsWith("Slates:")).length, 1);
  assert.ok(text.indexOf("Slates:") < text.indexOf(ELI5_CLOSING));
  assert.doesNotMatch(buildEli5([], [recent({})], slate({ settled: 0 })), /Slates:/);
});

// --- the command ----------------------------------------------------------------------------------

Deno.test("/eli5 replies from the scoreboard, the recent window and the slate counts, audits it and calls no model", async () => {
  const kit = makeKit();
  kit.store.score = [score({ arm: "B", arm_version: "rule-v1", picks_settled: 100, picks_right: 60, a_right_same_fixtures: 48 }), score({})];
  kit.store.recent = [
    recent({ picks_settled: 100, picks_right: 52, a_right_same_fixtures: 52 }),
    recent({ arm: "B", arm_version: "rule-v1", picks_settled: 100, picks_right: 64, a_right_same_fixtures: 52 }),
  ];
  kit.store.slate = slate({});
  const response = await post(kit, textMessage("/eli5"));
  assert.equal(response.status, 200);
  const text = kit.tg.sent.at(-1)!.text;
  assert.match(text, /Working: it is ahead of A by 12\.0 points/);
  assert.match(text, /Slates: for the 30 settled matches/);
  assert.ok(kit.store.audits.some((a) => a.action === "ELI5" && a.outcome === "OK"));
  assert.equal(kit.visionCalls.length, 0, "no vision or model call");
  assert.equal(kit.store.calls.size, 0, "no reading call is counted against the daily cap");
});

Deno.test("/eli5 with no data says it is too early; a failing slate view only drops the slate line", async () => {
  const kit = makeKit();
  await post(kit, textMessage("/eli5"));
  assert.match(kit.tg.sent.at(-1)!.text, /too early to tell about anything/);
  kit.store.score = [score({})];
  kit.store.recent = [recent({})];
  kit.store.failOn = "slateScore";
  const response = await post(kit, textMessage("/eli5", { updateId: 9002 }));
  assert.equal(response.status, 200);
  const text = kit.tg.sent.at(-1)!.text;
  assert.match(text, /A, the rating model: right 22 of 40/);
  assert.doesNotMatch(text, /Slates:/);
  assert.ok(kit.store.audits.some((a) => a.action === "ELI5" && /slate counts unavailable/.test(a.detail ?? "")));
});

Deno.test("/eli5 failing to read the scoreboard is a plain error, never a half message", async () => {
  const kit = makeKit();
  kit.store.failOn = "recentWindow";
  const response = await post(kit, textMessage("/eli5"));
  assert.equal(response.status, 200);
  assert.match(kit.tg.sent.at(-1)!.text, /Something went wrong/);
});

Deno.test("/eli5 is in the help and the menu, and the store reads the two views", async () => {
  assert.ok(helpText(10).includes("/eli5"));
  assert.ok(MENU.some((m) => m.command === "eli5"));
  const seen: string[] = [];
  const fn = ((url: string | URL | Request) => {
    seen.push(String(url));
    const body = String(url).includes("intake_slate_score")
      ? [{ slate_reads: 1, fixtures: 5, settled: 3, model_hits: 2, compared: 0, compared_model_hits: 0, compared_market_hits: 0 }]
      : [];
    return Promise.resolve(new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } }));
  }) as typeof fetch;
  const store = new PostgrestStore("https://p.example", "k".repeat(40), fn);
  assert.deepEqual(await store.recentWindow(), []);
  assert.equal((await store.slateScore())!.settled, 3);
  assert.deepEqual(seen.map((u) => new URL(u).pathname), ["/rest/v1/intake_recent_window", "/rest/v1/intake_slate_score"]);
});
