// Tests for the matching improvements recorded in DECISION 238 (addendum): apostrophes, aa/oe/ae spellings,
// words in a different order, the one-team anchor, the built-in seed names, and the reading prompt and schema.
// They run offline with no files: the 97 confirmed pairs are embedded in sample_pairs.ts.

import assert from "node:assert/strict";
import { anchorEnabled, buildAliases, matchRows, normalizeTeam, sameWords, seedAliases } from "./matching.ts";
import { SEED_ALIAS_PAIRS, SEED_VERSION } from "./seed_aliases.ts";
import { SAMPLE_PAIRS } from "./sample_pairs.ts";
import { fixture } from "./testkit.ts";
import { buildVisionRequest, parseVisionOutput, VISION_TOOL } from "./vision.ts";

const T0 = "2026-11-26T12:00:00Z";
const mk = (home: string, away: string, kickoff = T0) => ({
  home_team: home, away_team: away, kickoff_at: kickoff, home_odds: null, draw_odds: null, away_odds: null,
});
const ctx = { today: "2026-11-26", utcOffsetMinutes: 180 };

// --- the normaliser ----------------------------------------------------------------------------

Deno.test("apostrophes disappear, so a name with one equals the same name without", () => {
  assert.equal(normalizeTeam("Hapoel Be'er Sheva"), normalizeTeam("HAPOEL BEER SHEVA"));
  assert.equal(normalizeTeam("Borussia M'gladbach"), "borussia mgladbach");
  assert.equal(normalizeTeam("Hapoel Be’er Sheva"), normalizeTeam("Hapoel Beer Sheva")); // curly apostrophe
});

Deno.test("the aa, oe and ae spellings equal the plain letter, without damaging other names", () => {
  assert.equal(normalizeTeam("Vålerenga IF"), normalizeTeam("Vaalerenga IF"));
  assert.equal(normalizeTeam("Tromsø IL"), normalizeTeam("Tromsoe IL"));
  assert.equal(normalizeTeam("FC Nordsjælland"), normalizeTeam("FC Nordsjaelland"));
  assert.equal(normalizeTeam("Bayern München"), "bayern munchen");
  assert.equal(normalizeTeam("Stade Brest 29"), "stade brest 29"); // digits that belong to a name stay
  assert.equal(normalizeTeam("Ascoli Calcio 1898"), "ascoli calcio 1898");
  assert.notEqual(normalizeTeam("Real Madrid"), normalizeTeam("Real Madrid Castilla"));
});

Deno.test("the same words in a different order are the same name", () => {
  assert.equal(sameWords(normalizeTeam("Volos NPS"), normalizeTeam("NPS Volos")), true);
  assert.equal(sameWords("lille", "lille"), false); // one word equal is plain equality, not reordering
  assert.equal(sameWords("real madrid", "real betis"), false);
  assert.equal(sameWords("real madrid", "real madrid castilla"), false);
  const [m] = matchRows([mk("Volos NPS", "Panetolikos")], [fixture("v", "NPS Volos", "Panetolikos", T0)]);
  assert.deepEqual([m.fixtureId, m.via], ["v", "EXACT"]);
});

Deno.test("normalised names match across the spellings a betting site uses", () => {
  const [m] = matchRows([mk("Vaalerenga IF", "Tromsoe IL"), ], [fixture("n", "Vålerenga IF", "Tromsø IL", T0)]);
  assert.deepEqual([m.fixtureId, m.via], ["n", "EXACT"]);
  const [b] = matchRows([mk("HAPOEL BEER SHEVA", "Celta")], [fixture("b", "Hapoel Be'er Sheva", "Celta", T0)]);
  assert.deepEqual([b.fixtureId, b.via], ["b", "EXACT"]);
});

// --- the one-team anchor -----------------------------------------------------------------------

Deno.test("one-team anchor: one team fits exactly one fixture in the window, so the other name is accepted", () => {
  const stored = [
    fixture("hearts", "Heart of Midlothian", "AS Monaco", T0),
    fixture("other", "Alpha", "Beta", T0),
    fixture("late", "Gamma", "Delta", "2026-11-26T20:00:00Z"),
  ];
  const [homeSide, awaySide, neither, reversed, outside, wrongTime] = matchRows([
    mk("Heart of Midlothian", "Zzzz Unknown"),
    mk("Zzzz Unknown", "AS Monaco"),
    mk("Zzzz", "Yyyy"),
    mk("AS Monaco", "Heart of Midlothian"), // home and away swapped: no side binds
    mk("Heart of Midlothian", "Zzzz Unknown", "2026-11-26T15:00:01Z"), // just outside three hours
    mk("Gamma", "Zzzz"), // Gamma plays at 20:00, outside the window of a 12:00 row
  ], stored);
  for (const m of [homeSide, awaySide]) {
    assert.deepEqual([m.fixtureId, m.via, m.matchedTo], ["hearts", "ANCHOR", "Heart of Midlothian v AS Monaco"]);
  }
  for (const m of [neither, reversed, outside, wrongTime]) assert.deepEqual([m.fixtureId, m.reason, m.via], [null, "NO_MATCH", null]);
});

Deno.test("one-team anchor: a similar or remembered side also anchors; ambiguity and a full match are respected", () => {
  const stored = [fixture("b1", "Brighton & Hove Albion", "Universitatea Craiova", T0)];
  const [similar] = matchRows([mk("BRIGHTON", "Zzzz")], stored);
  assert.deepEqual([similar.fixtureId, similar.via], ["b1", "ANCHOR"]);
  const [remembered] = matchRows([mk("Zzzz", "Craiova U")], stored, new Map([["craiova u", "universitatea craiova"]]));
  assert.deepEqual([remembered.fixtureId, remembered.via], ["b1", "ANCHOR"]);

  // two fixtures that each fit one team: never guessed
  const twoHalves = [fixture("x1", "Atletico Madrid", "Foo", T0), fixture("x2", "Atletico Madrid", "Bar", T0)];
  const [ambiguous] = matchRows([mk("Atletico Madrid", "Zzzz")], twoHalves);
  assert.deepEqual([ambiguous.fixtureId, ambiguous.reason], [null, "AMBIGUOUS"]);

  // a fixture that fits both names wins over fixtures that fit one name: the anchor is only a fallback
  const mixed = [fixture("full", "Alpha", "Beta", T0), fixture("half", "Alpha", "Gamma", T0)];
  const [exact] = matchRows([mk("Alpha", "Beta")], mixed);
  assert.deepEqual([exact.fixtureId, exact.via], ["full", "EXACT"]);
});

Deno.test("one-team anchor: the setting switches it off and the same rows are then unmatched", () => {
  const stored = [fixture("hearts", "Heart of Midlothian", "AS Monaco", T0)];
  const rows = [mk("Heart of Midlothian", "Zzzz Unknown")];
  assert.equal(matchRows(rows, stored, new Map(), { anchor: true })[0].via, "ANCHOR");
  assert.equal(matchRows(rows, stored)[0].via, "ANCHOR", "on by default");
  const [off] = matchRows(rows, stored, new Map(), { anchor: false });
  assert.deepEqual([off.fixtureId, off.reason, off.via], [null, "NO_MATCH", null]);
});

Deno.test("the setting is on unless it says off; an odd value never switches matching off by accident", () => {
  assert.equal(anchorEnabled(undefined), true);
  assert.equal(anchorEnabled(""), true);
  for (const on of ["on", "1", "true", "yes", "garbage"]) assert.equal(anchorEnabled(on), true, on);
  for (const off of ["off", "OFF", " off ", "0", "false", "False", "no"]) assert.equal(anchorEnabled(off), false, off);
});

// --- the built-in seed names -------------------------------------------------------------------

Deno.test("every seed name is evidenced by a confirmed sample pair, and the list is sane", () => {
  assert.match(SEED_VERSION, /^seed-v\d+$/);
  assert.ok(SEED_ALIAS_PAIRS.length >= 20 && SEED_ALIAS_PAIRS.length <= 40, `${SEED_ALIAS_PAIRS.length} seed names`);
  const keys = SEED_ALIAS_PAIRS.map(([printed]) => normalizeTeam(printed));
  assert.equal(new Set(keys).size, keys.length, "no two seed names normalise to the same key");
  const counters = ["", " 16", " 23", " 25"]; // a market counter may follow the printed name on a listing
  const evidenced = (printed: string, stored: string) =>
    SAMPLE_PAIRS.some((p) =>
      [[p.printedHome, p.storedHome], [p.printedAway, p.storedAway]].some(([pp, ss]) =>
        counters.some((c) => normalizeTeam(pp) === normalizeTeam(printed) + c) && normalizeTeam(ss) === normalizeTeam(stored)
      )
    );
  for (const [printed, stored] of SEED_ALIAS_PAIRS) {
    assert.notEqual(normalizeTeam(printed), normalizeTeam(stored), `${printed} needs no seed`);
    assert.ok(evidenced(printed, stored), `${printed} = ${stored} is not backed by a confirmed pair`);
  }
});

Deno.test("seed names and the matching memory are read together; the memory wins and an unsafe memory removes a seed", () => {
  assert.equal(buildAliases([]).get("lyon"), "olympique lyonnais");
  assert.equal(seedAliases().get("lyon"), "olympique lyonnais");
  const h = (home: string, away: string, m: [string, string]) => ({ home_team: home, away_team: away, matched: { home_team: m[0], away_team: m[1] } });
  assert.equal(buildAliases([h("Lyon", "X", ["Lyon Duchere", "X"])]).get("lyon"), "lyon duchere", "memory wins over the seed");
  const unsafe = buildAliases([h("Lyon", "X", ["Lyon Duchere", "X"]), h("Lyon", "Y", ["Olympique Lyonnais", "Y"])]);
  assert.equal(unsafe.has("lyon"), false, "a name that led to two places is not trusted, seed or not");
});

// --- the 97 confirmed pairs --------------------------------------------------------------------

const BASE = Date.parse("2026-11-01T00:00:00Z");
const at = (i: number) => new Date(BASE + i * 5 * 3_600_000).toISOString(); // five hours apart: windows never overlap
const stored = SAMPLE_PAIRS.map((p, i) => fixture(`p${p.id}`, p.storedHome, p.storedAway, at(i)));
const rows = SAMPLE_PAIRS.map((p, i) => mk(p.printedHome, p.printedAway, at(i)));

function resolve(aliases: Map<string, string>, anchor: boolean) {
  const out = matchRows(rows, stored, aliases, { anchor });
  const right = new Set(out.flatMap((m, i) => (m.fixtureId === `p${SAMPLE_PAIRS[i].id}` ? [SAMPLE_PAIRS[i].id] : [])));
  const wrong = out.filter((m, i) => m.fixtureId !== null && m.fixtureId !== `p${SAMPLE_PAIRS[i].id}`).length;
  return { out, right, wrong };
}

Deno.test("at least 94 of the 97 confirmed pairs resolve, none to the wrong fixture, and every match says how", () => {
  assert.equal(SAMPLE_PAIRS.length, 97);
  const all = resolve(seedAliases(), true);
  assert.ok(all.right.size >= 94, `only ${all.right.size} of 97 resolved`);
  assert.equal(all.wrong, 0, "a pair matched to a different fixture");
  const vias = new Set(all.out.filter((m) => m.fixtureId !== null).map((m) => m.via));
  assert.ok(all.out.every((m) => (m.fixtureId === null) === (m.via === null || m.via === undefined)), "every match reports its method");
  for (const via of ["EXACT", "ALIAS", "LOOSE", "ANCHOR"]) assert.ok(vias.has(via as never), `no ${via} match among the pairs`);
  const baseline = resolve(new Map(), false);
  assert.ok(all.right.size - baseline.right.size >= 20, `the new rules add ${all.right.size - baseline.right.size}: expected 20 or more`);
});

Deno.test("the three known hard pairs resolve through the seed list", () => {
  const hard = [129, 153, 161]; // Stade Brest 29 v Olympique Lyon; Lille OSC v Olympique Lyon; Panaitolikos Agrinio v Volos NPS
  const withSeeds = resolve(seedAliases(), false); // with the one-team rule off, so only names and seeds can do it
  for (const id of hard) assert.ok(withSeeds.right.has(id), `pair ${id} does not resolve through the seed list`);
  const withoutSeeds = resolve(new Map(), true);
  for (const id of [129, 153]) assert.equal(withoutSeeds.right.has(id), false, `pair ${id} should need the seed list`);
});

Deno.test("a name with a real trailing number is left intact while a market counter is not the matcher's business", () => {
  const parsed = parseVisionOutput({
    fixtures: [
      { home_team: "Stade Brest 29", away_team: "Ascoli Calcio 1898" },
      { home_team: "MARA SUGAR FC", away_team: "KENYA POLICE FC 16" }, // the prompt leaves counters out; the parser never guesses
    ],
  }, ctx);
  assert.deepEqual(parsed.rows.map((r) => [r.home_team, r.away_team]), [["Stade Brest 29", "Ascoli Calcio 1898"], ["MARA SUGAR FC", "KENYA POLICE FC 16"]]);
  const [m] = matchRows([mk("Stade Brest 29", "Olympique Lyon")], [fixture("f", "Stade Brestois", "Olympique Lyonnais", T0)], seedAliases());
  assert.deepEqual([m.fixtureId, m.via], ["f", "ALIAS"]);
});

// --- the reading prompt and schema -------------------------------------------------------------

Deno.test("the reading prompt leaves out market counters, keeps real digits, and asks for country and final score", () => {
  const req = buildVisionRequest({ model: "m", mediaType: "application/pdf", base64: "AAAA", today: "2026-11-26" });
  assert.match(req.system, /market counter/i);
  assert.match(req.system, /16, 23 or 25/);
  assert.match(req.system, /Stade Brest 29/);
  assert.match(req.system, /country/i);
  assert.match(req.system, /final score/i);
  const props = VISION_TOOL.input_schema.properties.fixtures.items.properties as Record<string, unknown>;
  for (const key of ["country", "home_score", "away_score"]) assert.ok(key in props, key);
  assert.deepEqual(VISION_TOOL.input_schema.properties.fixtures.items.required, ["home_team", "away_team"]);
});

Deno.test("country and final score are read when valid and dropped otherwise", () => {
  const parsed = parseVisionOutput({
    fixtures: [
      { home_team: "A", away_team: "B", country: " Scotland ", home_score: 3, away_score: 1 },
      { home_team: "C", away_team: "D", country: "<b>", home_score: 3 }, // bad country, one score only
      { home_team: "E", away_team: "F", home_score: "3", away_score: 1 },
      { home_team: "G", away_team: "H", home_score: -1, away_score: 100 },
      { home_team: "I", away_team: "J" },
    ],
  }, ctx);
  const got = parsed.rows.map((r) => [r.country ?? null, r.home_score ?? null, r.away_score ?? null]);
  assert.deepEqual(got, [["Scotland", 3, 1], [null, null, null], [null, null, null], [null, null, null], [null, null, null]]);
});
