// /eli5: the plain-language performance message. Two windows (7 and 28 days) judged by one rule, the comparison
// line, ticket spikes either way, the 20-pick floor, the margin of error, the wording (never stakes, profit or
// advice), the slate line, and the command's wiring. Everything runs on fakes: no network, no clock, no secrets.

import assert from "node:assert/strict";
import { handleRequest } from "./handler.ts";
import { COMMANDS, helpText, MENU } from "./commands.ts";
import {
  buildEli5,
  compareWindows,
  ELI5_CLOSING,
  findSpikes,
  judge,
  marginPoints,
  MIN_SETTLED_FOR_VERDICT,
  slateLine,
  SPIKE_MAX_SHOWN,
  spikeLines,
} from "./eli5.ts";
import type { ScoreRow, SlateScoreRow, TicketHitRow, WindowRow } from "./store.ts";
import { makeKit, NOW, textMessage, webhook } from "./testkit.ts";
import { PostgrestStore } from "./store.ts";

type Kit = ReturnType<typeof makeKit>;
const post = (kit: Kit, update: unknown) => handleRequest(webhook(update), kit.deps);

const CLOCK = new Date("2026-11-28T12:00:00Z");

const win = (over: Partial<WindowRow>): WindowRow => ({
  ticket_kind: "SIMULATED", arm: "A", arm_version: "elo_davidson_recalibrated", window_name: "LAST28",
  picks_settled: 40, picks_right: 22, a_right_same_fixtures: 22, ...over,
});
/** The same arm's LAST7 and LAST28 rows at once. */
const both = (over: Partial<WindowRow>, last7: Partial<WindowRow>): WindowRow[] => [
  win({ ...over }),
  win({ ...over, window_name: "LAST7", ...last7 }),
];
const score = (over: Partial<ScoreRow>): ScoreRow => ({
  ticket_kind: "SIMULATED", arm: "A", arm_version: "elo_davidson_recalibrated", tickets: 3, picks_settled: 90, picks_right: 45,
  a_right_same_fixtures: 45, c_right_same_fixtures: 40, adjusted_settled: 0, adjusted_right: 0, a_right_on_adjusted: 0, ...over,
});
const slate = (over: Partial<SlateScoreRow>): SlateScoreRow => ({
  slate_reads: 2, fixtures: 40, settled: 30, model_hits: 14, compared: 10, compared_model_hits: 5, compared_market_hits: 6, ...over,
});
let ticketNo = 0;
const hit = (over: Partial<TicketHitRow>): TicketHitRow => ({
  ticket_id: `t${++ticketNo}`, ticket_kind: "SIMULATED", ticket_date: "2026-11-26", arm: "C", arm_version: "draw-score-v1",
  fixture_count: 20, picks_settled: 20, picks_right: 10, last_kickoff_at: "2026-11-26T15:00:00Z", ...over,
});
/** Three ordinary tickets for one arm: 60 settled picks, 30 right (a usual rate of exactly 50%). */
const baseline = (over: Partial<TicketHitRow> = {}): TicketHitRow[] => [0, 1, 2].map((i) => hit({ ...over, ticket_id: `base-${over.arm ?? "C"}-${over.ticket_kind ?? "S"}-${i}` }));

// words that would give advice, stakes or money talk; none may appear anywhere in the message
const ADVICE = /\b(stakes?|bankroll|profits?|winnings?|losses|you should|should you|bet on|back (?:it|them)|recommend\w*|guarantee\w*|sure thing|tip)\b/i;

// --- verdicts (one rule for both windows) ------------------------------------------------------------

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
  const v = judge({ picks_settled: 100, picks_right: 62, a_right_same_fixtures: 50 });
  assert.equal(v.kind, "working");
  assert.equal(v.kind === "working" && v.margin, 10);
});

Deno.test("dropping: at least 20 settled picks and behind A by more than the margin", () => {
  assert.equal(judge({ picks_settled: 100, picks_right: 38, a_right_same_fixtures: 50 }).kind, "dropping");
});

Deno.test("behind A by less than the margin is not dropping, however many picks", () => {
  const v = judge({ picks_settled: 100, picks_right: 45, a_right_same_fixtures: 50 });
  assert.equal(v.kind, "too_early");
  assert.equal(v.kind === "too_early" && v.reason, "inside_margin");
  assert.equal(judge({ picks_settled: 100, picks_right: 40, a_right_same_fixtures: 50 }).kind, "too_early");
  assert.equal(judge({ picks_settled: 100, picks_right: 39, a_right_same_fixtures: 50 }).kind, "dropping");
});

Deno.test("a gap exactly at the margin is not a verdict (it has to be larger)", () => {
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

// --- two windows and the comparison --------------------------------------------------------------------

Deno.test("each window gets its own verdict, and the comparison line says how they differ", () => {
  const text = buildEli5(
    [score({}), score({ arm: "B", arm_version: "rule-v1", picks_settled: 100, picks_right: 64, a_right_same_fixtures: 52 })],
    [
      ...both({}, {}),
      // 28 days: 100 settled, 12 points ahead of A (working); 7 days: only 15 settled picks (too early)
      ...both({ arm: "B", arm_version: "rule-v1", picks_settled: 100, picks_right: 64, a_right_same_fixtures: 52 }, { picks_settled: 15, picks_right: 12, a_right_same_fixtures: 6 }),
    ],
    null, null, CLOCK,
  );
  assert.match(text, /B1, your rule v1: last 7 days right 12 of 15 \(80%\); last 28 days right 64 of 100 \(64%\)\./);
  assert.match(text, /\n {2}Last 7 days: Too early to tell: only 15 settled picks, and I wait for 20\./);
  assert.match(text, /\n {2}Last 28 days: Working: it is ahead of A by 12\.0 points on the same 100 matches/);
  assert.match(text, /\n {2}This week differs from the month: too early to tell over 7 days, working over 28 days\. The 7-day window has only 15 settled picks\./);
});

Deno.test("the same verdict over both windows is reported as the same story", () => {
  const up = { picks_settled: 100, picks_right: 64, a_right_same_fixtures: 52 };
  const down = { picks_settled: 100, picks_right: 38, a_right_same_fixtures: 52 };
  const text = buildEli5([], [
    ...both({ arm: "B", arm_version: "rule-v1", ...up }, up),
    ...both({ arm: "C", arm_version: "draw-score-v1", ...down }, down),
    ...both({ arm: "B", arm_version: "rule-v2", picks_settled: 5, picks_right: 3, a_right_same_fixtures: 3 }, { picks_settled: 2, picks_right: 1, a_right_same_fixtures: 1 }),
  ], null, null, CLOCK);
  assert.match(text, /Same story over 7 and 28 days: working\./);
  assert.match(text, /Same story over 7 and 28 days: dropping\./);
  assert.match(text, /Both windows say too early to tell\./);
});

Deno.test("opposite verdicts in the two windows are both shown, and a thin 28-day window is named", () => {
  const up = { picks_settled: 100, picks_right: 64, a_right_same_fixtures: 52 };
  const down = { picks_settled: 100, picks_right: 38, a_right_same_fixtures: 52 };
  const text = buildEli5([], both({ arm: "B", arm_version: "rule-v1", ...down }, up), null, null, CLOCK);
  assert.match(text, /This week differs from the month: working over 7 days, dropping over 28 days\.(?! The)/);
  // direct: a 28-day window with only 6 picks, a 7-day window that is also thin, names the 7-day one first
  const thin28 = compareWindows(judge({ picks_settled: 100, picks_right: 64, a_right_same_fixtures: 52 }), win({ picks_settled: 100 }), judge({ picks_settled: 6, picks_right: 6, a_right_same_fixtures: 0 }), win({ picks_settled: 6 }));
  assert.equal(thin28, "This week differs from the month: working over 7 days, too early to tell over 28 days. The 28-day window has only 6 settled picks.");
});

Deno.test("arm A is the yardstick over both windows and never gets a verdict", () => {
  const text = buildEli5([score({})], both({}, { picks_settled: 10, picks_right: 6, a_right_same_fixtures: 6 }), null, null, CLOCK);
  const lines = text.split("\n");
  const at = lines.findIndex((l) => l.startsWith("- A, the rating model"));
  assert.equal(lines[at], "- A, the rating model: last 7 days right 6 of 10 (60%); last 28 days right 22 of 40 (55%).");
  assert.doesNotMatch(lines.slice(at, at + 3).join("\n"), /Working|Dropping|Too early|Same story|differs/);
});

Deno.test("an arm with nothing settled in a window says so, and is too early to tell", () => {
  const text = buildEli5(
    [score({ arm: "B", arm_version: "rule-v3", picks_settled: 100, picks_right: 70, a_right_same_fixtures: 40 })],
    [],
    null, null, CLOCK,
  );
  assert.match(text, /B3, your rule v3: last 7 days nothing settled; last 28 days nothing settled\./);
  assert.match(text, /Last 7 days: Too early to tell: only 0 settled picks/);
  assert.match(text, /Both windows say too early to tell\./);
  assert.doesNotMatch(text, /Working:|Dropping:/);
});

Deno.test("the 28 days before and the whole history are shown as context and never decide a verdict", () => {
  const text = buildEli5(
    [score({ arm: "C", arm_version: "draw-score-v1", picks_settled: 120, picks_right: 50 })],
    [
      ...both({ arm: "C", arm_version: "draw-score-v1", picks_settled: 30, picks_right: 12, a_right_same_fixtures: 12 }, { picks_settled: 30, picks_right: 12, a_right_same_fixtures: 12 }),
      win({ arm: "C", arm_version: "draw-score-v1", window_name: "PRIOR28", picks_settled: 100, picks_right: 80, a_right_same_fixtures: 10 }),
    ],
    null, null, CLOCK,
  );
  assert.match(text, /The 28 days before: right 80 of 100 \(80%\)/);
  assert.match(text, /Since the start: right 50 of 120 \(42%\)/);
  assert.match(text, /Last 28 days: Too early to tell: its gap from A/);
});

Deno.test("with nothing settled at all it says it is too early to tell about anything", () => {
  const text = buildEli5([], [], null, [], CLOCK);
  assert.match(text, /Nothing has settled yet, so it is too early to tell about anything/);
  assert.doesNotMatch(text, /Working:|Dropping:|Ticket check|stood out/);
});

// --- ticket spikes -------------------------------------------------------------------------------------

Deno.test("a ticket far above its arm's usual rate is called unusually high, with its baseline", () => {
  const spiky = hit({ picks_settled: 16, picks_right: 13, ticket_date: "2026-11-27", last_kickoff_at: "2026-11-27T15:00:00Z" });
  const { spikes, checked } = findSpikes([...baseline(), spiky], CLOCK);
  assert.equal(spikes.length, 1);
  assert.equal(spikes[0].ticketId, spiky.ticket_id);
  assert.equal(spikes[0].direction, "high");
  assert.equal(spikes[0].baselinePicks, 60);
  assert.equal(spikes[0].baselineTickets, 3);
  assert.equal(checked, 4, "every ticket here has 8 settled picks and a baseline of the other three, so all four are checked");
});

Deno.test("high and low spikes are both found, either way, and shown newest first with the chance note", () => {
  const base = baseline();
  const high = hit({ ticket_id: "high", picks_settled: 16, picks_right: 13, ticket_date: "2026-11-27", last_kickoff_at: "2026-11-27T15:00:00Z" });
  const low = hit({ ticket_id: "low", picks_settled: 16, picks_right: 3, ticket_date: "2026-11-26", last_kickoff_at: "2026-11-26T18:00:00Z" });
  const check = findSpikes([...base, low, high], CLOCK);
  assert.deepEqual(check.spikes.map((s) => [s.ticketId, s.direction]), [["high", "high"], ["low", "low"]]);
  const lines = spikeLines(check);
  assert.match(lines[0], /^Tickets that stood out \(a ticket's hit rate against the same arm's other tickets of that kind over the last 28 days\):$/);
  assert.match(lines[1], /^- Simulated ticket 2026-11-27, C: right 13 of 16 \(81%\), unusually high against its usual \d+% over 4 other tickets \(\d+ picks\)\.$/);
  assert.match(lines[2], /^- Simulated ticket 2026-11-26, C: right 3 of 16 \(19%\), unusually low against its usual/);
  assert.equal(lines.at(-1), "About 1 ticket in 20 looks like this by chance alone, so read one as a prompt to look, not as a finding.");
});

Deno.test("a ticket exactly two standard errors away is not a spike; more than two is", () => {
  // baseline 30 right of 60 (50%); a 16-pick ticket has a standard error of 0.125, so 12 right is exactly +2.0
  assert.equal(findSpikes([...baseline(), hit({ picks_settled: 16, picks_right: 12 })], CLOCK).spikes.length, 0);
  assert.equal(findSpikes([...baseline(), hit({ picks_settled: 16, picks_right: 13 })], CLOCK).spikes.length, 1);
  assert.equal(findSpikes([...baseline(), hit({ picks_settled: 16, picks_right: 4 })], CLOCK).spikes.length, 0); // exactly -2.0
  assert.equal(findSpikes([...baseline(), hit({ picks_settled: 16, picks_right: 3 })], CLOCK).spikes.length, 1);
});

Deno.test("a ticket with fewer than 8 settled picks is never checked", () => {
  const check = findSpikes([...baseline(), hit({ picks_settled: 7, picks_right: 7 })], CLOCK);
  assert.deepEqual(check.spikes, []);
  assert.equal(check.checked, 3 * 0 + check.checked); // the base tickets (20 picks each) are checked against each other
  assert.equal(findSpikes([...baseline(), hit({ picks_settled: 8, picks_right: 8 })], CLOCK).spikes.length, 1);
});

Deno.test("without 3 other tickets and 40 settled picks there is no baseline, so no spike can be called", () => {
  // two other tickets only (even with plenty of picks)
  const two = [hit({ picks_settled: 40, picks_right: 20 }), hit({ picks_settled: 40, picks_right: 20 }), hit({ picks_settled: 16, picks_right: 16 })];
  assert.deepEqual(findSpikes(two, CLOCK), { spikes: [], checked: 0 });
  // three other tickets but only 39 settled picks between them: the 16-pick ticket cannot be judged (only the
  // three small tickets, which each have the other small ones plus it as a baseline, are checked)
  const few = [hit({ picks_settled: 13, picks_right: 6 }), hit({ picks_settled: 13, picks_right: 6 }), hit({ picks_settled: 13, picks_right: 6 }), hit({ ticket_id: "big", picks_settled: 16, picks_right: 16 })];
  const fewCheck = findSpikes(few, CLOCK);
  assert.equal(fewCheck.spikes.some((s) => s.ticketId === "big"), false);
  assert.equal(fewCheck.checked, 3);
  // 40 picks is enough
  const enough = [hit({ picks_settled: 14, picks_right: 7 }), hit({ picks_settled: 13, picks_right: 6 }), hit({ picks_settled: 13, picks_right: 7 }), hit({ ticket_id: "big", picks_settled: 16, picks_right: 16 })];
  assert.deepEqual(findSpikes(enough, CLOCK).spikes.map((s) => s.ticketId), ["big"]);
});

Deno.test("a ticket is left out of its own baseline", () => {
  // three tickets of 20 picks: two ordinary (10 right) and one at 20 right. Against the other two it is a spike;
  // had it been in its own baseline the baseline would be 40 of 60 and it would not be.
  const tickets = [hit({ ticket_id: "a", picks_right: 10 }), hit({ ticket_id: "b", picks_right: 10 }), hit({ ticket_id: "c", picks_right: 10 }), hit({ ticket_id: "z", picks_right: 20 })];
  const check = findSpikes(tickets, CLOCK);
  assert.deepEqual(check.spikes.map((s) => s.ticketId), ["z"]);
  assert.equal(check.spikes[0].baselineTickets, 3);
  assert.equal(check.spikes[0].baselinePicks, 60);
  assert.equal(check.spikes[0].baselineRate, 0.5);
});

Deno.test("only the same arm, the same rule version and the same ticket kind make a baseline", () => {
  const spiky = hit({ picks_settled: 16, picks_right: 16 });
  // baselines of other kinds, other arms and other versions do not count
  const wrong = [
    ...baseline({ ticket_kind: "SCREENSHOT" }), ...baseline({ arm: "A", arm_version: "elo_davidson_recalibrated" }), ...baseline({ arm: "B", arm_version: "rule-v2" }),
  ];
  assert.deepEqual(findSpikes([...wrong, spiky], CLOCK), { spikes: [], checked: 0 });
  assert.equal(findSpikes([...baseline(), spiky], CLOCK).spikes.length, 1);
  // two versions of the same rule arm are separate arms: rule v2's tickets are not rule v1's baseline
  const v2 = baseline({ arm: "B", arm_version: "rule-v2" });
  const v1Spiky = hit({ arm: "B", arm_version: "rule-v1", picks_settled: 16, picks_right: 16 });
  assert.deepEqual(findSpikes([...v2, v1Spiky], CLOCK).spikes.some((s) => s.ticketId === v1Spiky.ticket_id), false);
  const v1Base = baseline({ arm: "B", arm_version: "rule-v1" });
  assert.deepEqual(findSpikes([...v1Base, v1Spiky], CLOCK).spikes.map((s) => s.ticketId), [v1Spiky.ticket_id]);
});

Deno.test("tickets whose settled kickoffs are older than 28 days are neither checked nor used as a baseline", () => {
  const old = baseline().map((h) => ({ ...h, last_kickoff_at: "2026-10-01T15:00:00Z" }));
  assert.deepEqual(findSpikes([...old, hit({ picks_settled: 16, picks_right: 16 })], CLOCK), { spikes: [], checked: 0 });
  assert.deepEqual(findSpikes([...baseline(), hit({ picks_settled: 16, picks_right: 16, last_kickoff_at: "2026-10-01T15:00:00Z" })], CLOCK).spikes, []);
});

Deno.test("a baseline where every pick was right or none was has no spread, so it is skipped", () => {
  // the candidate's own baseline is the three others only: all of them right, or none, so it cannot be measured
  const allRight = [0, 1, 2].map((i) => hit({ ticket_id: `r${i}`, picks_right: 20 }));
  const cand = hit({ ticket_id: "cand", picks_settled: 16, picks_right: 4 });
  assert.equal(findSpikes([...allRight, cand], CLOCK).spikes.some((s) => s.ticketId === "cand"), false);
  const noneRight = [0, 1, 2].map((i) => hit({ ticket_id: `n${i}`, picks_right: 0 }));
  assert.equal(findSpikes([...noneRight, { ...cand, picks_right: 16 }], CLOCK).spikes.some((s) => s.ticketId === "cand"), false);
  // while one right among the others restores a spread, and the candidate is then measured
  const some = [hit({ ticket_id: "s0", picks_right: 1 }), hit({ ticket_id: "s1", picks_right: 0 }), hit({ ticket_id: "s2", picks_right: 0 })];
  assert.equal(findSpikes([...some, { ...cand, picks_right: 16 }], CLOCK).spikes.some((s) => s.ticketId === "cand"), true);
});

Deno.test("at most five spikes are shown, newest first, with the rest counted", () => {
  // eight ordinary tickets older than the seven very good ones, so the very good ones are the newest
  const base = [0, 1, 2, 3, 4, 5, 6, 7].map((i) => hit({ ticket_id: `old-${i}`, ticket_date: "2026-11-10", last_kickoff_at: "2026-11-10T15:00:00Z" }));
  const spikes = [1, 2, 3, 4, 5, 6, 7].map((day) => hit({ ticket_id: `s${day}`, picks_settled: 16, picks_right: 16, ticket_date: `2026-11-2${day}`, last_kickoff_at: `2026-11-2${day}T15:00:00Z` }));
  const check = findSpikes([...base, ...spikes], CLOCK);
  assert.ok(check.spikes.length > SPIKE_MAX_SHOWN);
  const lines = spikeLines(check);
  const shown = lines.filter((l) => l.startsWith("- Simulated ticket"));
  assert.equal(shown.length, SPIKE_MAX_SHOWN);
  assert.match(shown[0], /2026-11-27/);
  assert.match(shown[1], /2026-11-26/);
  assert.ok(lines.includes(`- and ${check.spikes.length - SPIKE_MAX_SHOWN} more.`));
  assert.equal(SPIKE_MAX_SHOWN, 5);
});

Deno.test("with too few tickets the message says so instead of guessing; with enough and no spike it says none stood out", () => {
  assert.match(spikeLines({ spikes: [], checked: 0 })[0], /^Ticket check: not enough tickets yet to call a spike\. I need a ticket arm with 8 settled picks and 3 other tickets of the same kind with 40 settled picks between them, all in the last 28 days\.$/);
  assert.match(spikeLines({ spikes: [], checked: 4 })[0], /^Ticket check: no ticket stood out against its arm's usual rate \(4 ticket results checked\)\.$/);
  assert.match(spikeLines({ spikes: [], checked: 1 })[0], /\(1 ticket result checked\)/);
  assert.deepEqual(spikeLines(null), []);
});

Deno.test("the ticket check is part of the message when there is something settled, and left out when its view is unavailable", () => {
  const w = both({}, {});
  assert.match(buildEli5([score({})], w, null, [], CLOCK), /Ticket check: not enough tickets yet/);
  assert.match(buildEli5([score({})], w, null, [...baseline(), hit({ picks_settled: 16, picks_right: 16 })], CLOCK), /Tickets that stood out/);
  assert.doesNotMatch(buildEli5([score({})], w, null, null, CLOCK), /Ticket check|stood out/);
});

// --- wording -------------------------------------------------------------------------------------------

Deno.test("the message never talks about stakes, profit, advice or what to back, in any state", () => {
  const spikyHits = [...baseline(), hit({ picks_settled: 16, picks_right: 16 }), hit({ picks_settled: 16, picks_right: 1 })];
  const states: [ScoreRow[], WindowRow[], SlateScoreRow | null, TicketHitRow[] | null][] = [
    [[], [], null, []],
    [[score({})], both({}, {}), slate({}), []],
    [
      [score({ arm: "B", arm_version: "rule-v1" }), score({ arm: "C", arm_version: "draw-score-v1" })],
      [
        ...both({ arm: "B", arm_version: "rule-v1", picks_settled: 100, picks_right: 70, a_right_same_fixtures: 40 }, { picks_settled: 30, picks_right: 5, a_right_same_fixtures: 20 }),
        ...both({ arm: "C", arm_version: "draw-score-v1", picks_settled: 100, picks_right: 30, a_right_same_fixtures: 60 }, { picks_settled: 4, picks_right: 4, a_right_same_fixtures: 0 }),
      ],
      slate({ compared: 0 }),
      spikyHits,
    ],
  ];
  for (const [s, w, sl, h] of states) {
    const text = buildEli5(s, w, sl, h, CLOCK);
    assert.doesNotMatch(text, ADVICE);
    assert.ok(text.endsWith(ELI5_CLOSING));
    assert.match(text, /not betting advice/);
  }
});

Deno.test("the command's own help text also keeps clear of stakes and profit", () => {
  const spec = COMMANDS.find((c) => c.name === "eli5")!;
  assert.match(spec.summary, /never about stakes or profit/);
  assert.match(spec.summary, /20 settled picks/);
  assert.match(spec.summary, /last 7 and 28 days/);
  assert.match(spec.summary, /unusually high or low/);
  assert.doesNotMatch(spec.menu!, ADVICE);
});

// --- the slate line ------------------------------------------------------------------------------------

Deno.test("the slate line appears only once slates have settled, and uses counts only", () => {
  assert.equal(slateLine(null), null);
  assert.equal(slateLine(slate({ settled: 0 })), null);
  const line = slateLine(slate({}))!;
  assert.match(line, /^Slates: for the 30 settled matches on them \(each counted once\), the rating model's pick was right 14 times \(47%\)\./);
  assert.match(line, /Where all three prices were printed \(10\), the market's favourite was right 6 times and the rating model 5\./);
  assert.doesNotMatch(slateLine(slate({ compared: 0 }))!, /prices/);
  assert.match(slateLine(slate({ settled: 1, model_hits: 1, compared: 0 }))!, /for the 1 settled match on them/);
  const text = buildEli5([], both({}, {}), slate({}), [], CLOCK);
  assert.equal(text.split("\n").filter((l) => l.startsWith("Slates:")).length, 1);
  assert.ok(text.indexOf("Slates:") < text.indexOf(ELI5_CLOSING));
  assert.doesNotMatch(buildEli5([], both({}, {}), slate({ settled: 0 }), [], CLOCK), /Slates:/);
});

// --- the command ---------------------------------------------------------------------------------------

Deno.test("/eli5 replies from the scoreboard, the windows, the ticket hits and the slate counts, audits it and calls no model", async () => {
  const kit = makeKit();
  kit.store.score = [score({ arm: "B", arm_version: "rule-v1", picks_settled: 100, picks_right: 60, a_right_same_fixtures: 48 }), score({})];
  kit.store.windows = [
    ...both({ picks_settled: 100, picks_right: 52, a_right_same_fixtures: 52 }, { picks_settled: 30, picks_right: 15, a_right_same_fixtures: 15 }),
    ...both({ arm: "B", arm_version: "rule-v1", picks_settled: 100, picks_right: 64, a_right_same_fixtures: 52 }, { picks_settled: 60, picks_right: 40, a_right_same_fixtures: 30 }),
  ];
  kit.store.hits = [...baseline({ last_kickoff_at: "2026-11-25T15:00:00Z" }), hit({ picks_settled: 16, picks_right: 16, last_kickoff_at: "2026-11-26T15:00:00Z" })];
  kit.store.slate = slate({});
  const response = await post(kit, textMessage("/eli5"));
  assert.equal(response.status, 200);
  const text = kit.tg.sent.map((m) => m.text).join("\n");
  assert.match(text, /Last 28 days: Working: it is ahead of A by 12\.0 points/);
  assert.match(text, /Last 7 days: Working: it is ahead of A by 16\.7 points/);
  assert.match(text, /Same story over 7 and 28 days: working\./);
  assert.match(text, /Tickets that stood out/);
  assert.match(text, /Slates: for the 30 settled matches/);
  assert.ok(kit.store.audits.some((a) => a.action === "ELI5" && a.outcome === "OK"));
  assert.equal(kit.visionCalls.length, 0, "no vision or model call");
  assert.equal(kit.store.calls.size, 0, "no reading call is counted against the daily cap");
});

Deno.test("/eli5 with no data says it is too early; a failing slate or ticket view only drops its own section", async () => {
  const kit = makeKit();
  await post(kit, textMessage("/eli5"));
  assert.match(kit.tg.sent.at(-1)!.text, /too early to tell about anything/);
  kit.store.score = [score({})];
  kit.store.windows = both({}, {});
  kit.store.hits = [];
  kit.store.failOn = "slateScore";
  assert.equal((await post(kit, textMessage("/eli5", { updateId: 9002 }))).status, 200);
  const slateGone = kit.tg.sent.at(-1)!.text;
  assert.match(slateGone, /A, the rating model: last 7 days right 22 of 40/);
  assert.match(slateGone, /Ticket check: not enough tickets yet/);
  assert.doesNotMatch(slateGone, /Slates:/);
  assert.ok(kit.store.audits.some((a) => a.action === "ELI5" && /slate counts unavailable/.test(a.detail ?? "")));

  kit.store.failOn = "ticketHits";
  kit.store.slate = slate({});
  assert.equal((await post(kit, textMessage("/eli5", { updateId: 9003 }))).status, 200);
  const ticketGone = kit.tg.sent.at(-1)!.text;
  assert.match(ticketGone, /Slates: for the 30 settled matches/);
  assert.doesNotMatch(ticketGone, /Ticket check|stood out/);
  assert.ok(kit.store.audits.some((a) => a.action === "ELI5" && /ticket check unavailable/.test(a.detail ?? "")));
});

Deno.test("/eli5 failing to read the scoreboard or the windows is a plain error, never a half message", async () => {
  for (const name of ["scoreboard", "windowSummary"]) {
    const kit = makeKit();
    kit.store.failOn = name;
    const response = await post(kit, textMessage("/eli5"));
    assert.equal(response.status, 200);
    assert.match(kit.tg.sent.at(-1)!.text, /Something went wrong/);
  }
});

Deno.test("/eli5 uses the handler's clock: a ticket kicked off more than 28 days before 'now' is not checked", async () => {
  const kit = makeKit();
  kit.store.score = [score({})];
  kit.store.windows = both({}, {});
  const old = new Date(NOW.getTime() - 40 * 24 * 3600 * 1000).toISOString();
  kit.store.hits = [...baseline({ last_kickoff_at: old }), hit({ picks_settled: 16, picks_right: 16, last_kickoff_at: old })];
  await post(kit, textMessage("/eli5"));
  assert.match(kit.tg.sent.at(-1)!.text, /Ticket check: not enough tickets yet/);
});

Deno.test("/eli5 is in the help and the menu, and the store reads the three views", async () => {
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
  assert.deepEqual(await store.windowSummary(), []);
  assert.deepEqual(await store.ticketHits(), []);
  assert.equal((await store.slateScore())!.settled, 3);
  assert.deepEqual(seen.map((u) => new URL(u).pathname), ["/rest/v1/intake_window_summary", "/rest/v1/intake_ticket_hits", "/rest/v1/intake_slate_score"]);
});
