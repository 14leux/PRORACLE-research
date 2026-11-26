// Registers the bot's command menu with Telegram (setMyCommands), taken from the same list as /help.
// Run by the operator on their own machine; the Edge Function never imports this file.
//
//   deno run --allow-net=api.telegram.org --allow-env=TELEGRAM_BOT_TOKEN supabase/functions/telegram-intake/set_menu.ts
//   deno run supabase/functions/telegram-intake/set_menu.ts --print      (shows the menu, needs no token)
//
// The bot token is read from the environment and never printed; network errors are reported
// without their text because it can contain the request URL.
import { MENU } from "./commands.ts";

if (Deno.args.includes("--print")) {
  console.log(JSON.stringify(MENU, null, 2));
  Deno.exit(0);
}

const token = Deno.env.get("TELEGRAM_BOT_TOKEN");
if (!token) {
  console.error("TELEGRAM_BOT_TOKEN is not set in the environment.");
  Deno.exit(1);
}

try {
  const response = await fetch(`https://api.telegram.org/bot${token}/setMyCommands`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ commands: MENU }),
  });
  const body = await response.json().catch(() => ({})) as { ok?: boolean; description?: string };
  if (response.ok && body.ok === true) {
    console.log(`Menu set: ${MENU.map((m) => "/" + m.command).join(" ")}`);
  } else {
    console.error(`Telegram refused the menu (HTTP ${response.status}): ${body.description ?? "no description"}`);
    Deno.exit(1);
  }
} catch {
  console.error("Could not reach Telegram.");
  Deno.exit(1);
}
