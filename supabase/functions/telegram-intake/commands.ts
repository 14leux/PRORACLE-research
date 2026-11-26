// The single list of what the operator can do. /help is generated from it, a test fails when a
// command or a limit is missing from the help text, and the handler's routing table is checked
// against it, so the help cannot drift from the behaviour.

export interface CommandSpec {
  /** Stable key used by the handler's routing table. */
  readonly name: string;
  readonly usage: string;
  readonly summary: string;
  /** Short text for Telegram's command menu; present only on slash commands (3-256 characters). */
  readonly menu?: string;
}

export const MAX_FIXTURES = 20;
export const MAX_FILE_BYTES = 10 * 1024 * 1024;
export const PENDING_RULE_MINUTES = 60;
export const MAX_RULE_TEXT_CHARS = 300;

export const COMMANDS: readonly CommandSpec[] = [
  {
    name: "ticket_file",
    usage: "send a photo, an image file (PNG, JPEG, WebP) or a PDF of fixtures",
    summary:
      `I read up to ${MAX_FIXTURES} fixtures (teams, kickoff, odds), match them to stored fixtures, then freeze arms A, B and C before kickoff and reply here.`,
  },
  {
    name: "rule",
    usage: "/rule <directives separated by ;>",
    summary:
      "Propose a rule set for arm B, for example: /rule 5 draws; 4 upsets by margin; only Premier League, La Liga. Directives: N draws, N upsets by margin|odds, only <leagues>, skip <leagues>. I echo it back and it is used only after YES.",
    menu: "Propose a rule for arm B (confirm with YES)",
  },
  { name: "yes", usage: "YES", summary: `Activate the rule I last proposed (it waits ${PENDING_RULE_MINUTES} minutes).` },
  { name: "no", usage: "NO", summary: "Cancel the rule I last proposed." },
  {
    name: "rules",
    usage: "/rules",
    summary: "Show the active rule set and any proposal waiting for YES.",
    menu: "Show the active rule set",
  },
  {
    name: "ticket",
    usage: "/ticket",
    summary: "Show today's tickets and each arm's picks.",
    menu: "Today's tickets and each arm's picks",
  },
  {
    name: "scoreboard",
    usage: "/scoreboard",
    summary: "Hit rate of each arm on settled ticket picks. A calibration report card, not evidence of an edge.",
    menu: "Arm hit rates on settled picks",
  },
  { name: "help", usage: "/help", summary: "Show this list.", menu: "What I can do, limits and what I cannot do" },
];

/** Telegram's command menu (setMyCommands), derived from the same list so it cannot drift from /help. */
export const MENU: readonly { readonly command: string; readonly description: string }[] = COMMANDS
  .filter((c) => c.menu !== undefined)
  .map((c) => ({ command: c.name, description: c.menu as string }));

/** Things this bot will not do, listed in /help and covered by a test. */
export const CANNOT: readonly string[] = [
  "Change the model or arm A.",
  "Put odds into arm A or arm C (odds are used only by arm B and the market comparison).",
  "Give stake, bankroll or profit advice.",
  "Apply a rule change without a YES from you.",
  "Act for any other chat.",
];

export function limits(dailyCap: number): readonly string[] {
  return [
    `At most ${MAX_FIXTURES} fixtures per ticket; extra rows are dropped.`,
    `At most ${dailyCap} screenshot or PDF reads per UTC day.`,
    `Files up to ${Math.round(MAX_FILE_BYTES / (1024 * 1024))} MB. The file is read in memory and never stored; only teams, kickoffs and odds are kept.`,
    "A row is matched to a stored fixture by exact name, then by names I remember from earlier tickets, then by name similarity when exactly one fixture fits within 3 hours; anything else is reported, never guessed.",
  ];
}

export function helpText(dailyCap: number): string {
  const lines: string[] = ["What you can do:"];
  for (const c of COMMANDS) {
    lines.push(`- ${c.usage}: ${c.summary}`);
  }
  lines.push("", "Limits:");
  for (const l of limits(dailyCap)) lines.push(`- ${l}`);
  lines.push("", "What I cannot do:");
  for (const c of CANNOT) lines.push(`- ${c}`);
  return lines.join("\n");
}
