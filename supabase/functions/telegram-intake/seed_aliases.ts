// A versioned list of names the matcher cannot work out by itself: official names a betting site shortens or
// respells, English spellings of foreign clubs, and one renamed club. Each entry is [the name as a betting site
// prints it, the name as the fixtures are stored]. Every entry is backed by a confirmed pair in sample_pairs.ts
// (a test enforces it), so nothing here is a guess; add an entry only when a real ticket has shown the pair.
//
// The list is read together with the matching memory (matching.ts: buildAliases). The memory wins where it
// disagrees, and a name the memory has tied to two different places is dropped from the list as well.
// Names are written here as they appear on pages; matching.ts normalises both sides, so case, accents and
// punctuation do not matter. Keep this file free of imports: matching.ts imports it.

export const SEED_VERSION = "seed-v1";

export const SEED_ALIAS_PAIRS: readonly (readonly [string, string])[] = [
  // official or longer names
  ["CS U Craiova", "Universitatea Craiova"],
  ["RB Salzburg", "Red Bull Salzburg"],
  ["Olympiakos", "Olympiacos FC"],
  ["Hearts", "Heart of Midlothian"],
  ["Kups Kuopio", "Kuopion Palloseura"],
  ["TPS Turku", "Turun Palloseura"],
  ["OH Leuven", "Oud-Heverlee Leuven"],
  ["Nacional Madeira", "CD Nacional"],
  ["Maritimo Madeira", "CS Marítimo"],
  ["Panaitolikos Agrinio", "GFS Panetolikos"],
  ["Racing Santander", "Real Racing Club"],
  ["SV Darmstadt", "Darmstadt 98"],
  ["Lille OSC", "LOSC Lille"],
  ["Brest", "Stade Brestois"],
  ["Stade Brest 29", "Stade Brestois"],
  ["Lyon", "Olympique Lyonnais"],
  ["Olympique Lyon", "Olympique Lyonnais"],
  // English spellings of foreign clubs
  ["1. FC Cologne", "1. FC Köln"],
  ["Sparta Prague", "AC Sparta Praha"],
  ["Red Star Belgrade", "FK Crvena zvezda"],
  ["FC Copenhagen", "FC København"],
  // a renamed club: the betting site still prints the old name
  ["Saburtalo Tbilisi", "FC Iberia 1999"],
];
