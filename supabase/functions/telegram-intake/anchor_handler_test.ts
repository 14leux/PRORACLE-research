// Handler-level tests for the matching improvements (DECISION 238 addendum): the one-team match is listed
// back and stored with its method, the setting really switches it off, and the built-in seed names still work
// when the matching memory cannot be read. Offline, with the same fakes as the other handler tests.

import assert from "node:assert/strict";
import { handleRequest } from "./handler.ts";
import { fileMessage, fixture, makeKit, row, webhook } from "./testkit.ts";

// 2026-11-26 15:00 in the default East Africa offset (+03:00) is 12:00 UTC.
const KICKOFF = "2026-11-26T12:00:00Z";

async function post(kit: ReturnType<typeof makeKit>, update: unknown) {
  return await handleRequest(webhook(update), kit.deps);
}

Deno.test("a one-team match is used, listed back for checking, stored with its method and counted", async () => {
  const kit = makeKit();
  kit.setVision({ fixtures: [row("Zzzz Unknown", "AS Monaco"), row("Alpha", "Beta"), row("Brighton", "Universitatea Craiova")] });
  kit.store.fixtures = [
    fixture("f1", "Heart of Midlothian", "AS Monaco", KICKOFF),
    fixture("f2", "Alpha", "Beta", KICKOFF),
    fixture("f3", "Brighton & Hove Albion", "Universitatea Craiova", KICKOFF),
  ];
  await post(kit, fileMessage("photo"));
  const text = kit.tg.texts;
  assert.match(text, /Matched 3, unmatched 0/);
  assert.match(text, /Matched by one team and kickoff \(check these; I will remember them for next time\):/);
  assert.match(text, /- Zzzz Unknown v AS Monaco = Heart of Midlothian v AS Monaco/);
  assert.match(text, /Matched by name similarity/);
  assert.doesNotMatch(text, /Alpha v Beta =/);
  const rows = kit.store.intakes[0].rows;
  assert.deepEqual(rows.map((r) => [r.fixtureId, r.via]), [["f1", "ANCHOR"], ["f2", "EXACT"], ["f3", "LOOSE"]]);
  assert.ok(rows.every((r) => r.via), "every matched row records how it matched");
  assert.equal(kit.store.audits.find((a) => a.action === "TICKET")!.detail, "read=3 kept=3 matched=3 remembered=0 similar=1 oneteam=1");
  assert.deepEqual(kit.dispatched, ["intake-1"]);
});

Deno.test("with the one-team rule switched off by the setting, the same row is unmatched and nothing is computed", async () => {
  const off = makeKit({ anchorMatching: false });
  off.setVision({ fixtures: [row("Zzzz Unknown", "AS Monaco")] });
  off.store.fixtures = [fixture("f1", "Heart of Midlothian", "AS Monaco", KICKOFF)];
  await post(off, fileMessage("photo"));
  assert.match(off.tg.texts, /Matched 0, unmatched 1/);
  assert.doesNotMatch(off.tg.texts, /one team and kickoff/);
  assert.deepEqual(off.store.intakes[0].rows.map((r) => [r.fixtureId, r.reason]), [[null, "NO_MATCH"]]);
  assert.deepEqual(off.dispatched, []);

  const on = makeKit({ anchorMatching: true });
  on.setVision({ fixtures: [row("Zzzz Unknown", "AS Monaco")] });
  on.store.fixtures = [fixture("f1", "Heart of Midlothian", "AS Monaco", KICKOFF)];
  await post(on, fileMessage("photo"));
  assert.match(on.tg.texts, /Matched 1, unmatched 0/);
});

Deno.test("an unreadable matching memory still leaves the built-in seed names working", async () => {
  const kit = makeKit();
  kit.store.failOn = "matchedHistory";
  kit.setVision({ fixtures: [row("Lille OSC", "Olympique Lyon")] });
  kit.store.fixtures = [fixture("f1", "LOSC Lille", "Olympique Lyonnais", KICKOFF)];
  await post(kit, fileMessage("photo"));
  assert.match(kit.tg.texts, /Matched 1, unmatched 0/);
  assert.deepEqual(kit.store.intakes[0].rows.map((r) => r.via), ["ALIAS"]);
  assert.ok(kit.store.audits.some((a) => a.action === "ERROR" && a.detail === "matching memory unreadable"));
});

Deno.test("a pair matched by one team is remembered: the next ticket matches the other name outright", async () => {
  const first = makeKit();
  first.setVision({ fixtures: [row("Zzzz Unknown", "AS Monaco")] });
  first.store.fixtures = [fixture("f1", "Heart of Midlothian", "AS Monaco", KICKOFF)];
  await post(first, fileMessage("photo"));
  assert.equal(first.store.intakes[0].rows[0].via, "ANCHOR");

  // the stored ticket is what the memory is read back from, so feed it back as history
  const second = makeKit({ anchorMatching: false });
  second.store.history = [{
    home_team: "Zzzz Unknown",
    away_team: "AS Monaco",
    matched: { home_team: "Heart of Midlothian", away_team: "AS Monaco" },
  }];
  second.setVision({ fixtures: [row("Zzzz Unknown", "AS Monaco")] });
  second.store.fixtures = [fixture("f9", "Heart of Midlothian", "AS Monaco", KICKOFF)];
  await post(second, fileMessage("photo"));
  assert.deepEqual(second.store.intakes[0].rows.map((r) => [r.fixtureId, r.via]), [["f9", "ALIAS"]]);
});
