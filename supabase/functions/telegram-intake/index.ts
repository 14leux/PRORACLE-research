// Supabase Edge Function entry point: wires the environment to the handler. Nothing is read from the
// request except the Telegram update; every secret comes from the function's secret store.
//
// Telegram cannot send a Supabase JWT, so JWT verification is off for this function in
// supabase/config.toml and the Telegram secret-token header is the authentication.

import { handleRequest, type Deps } from "./handler.ts";
import { anchorEnabled } from "./matching.ts";
import { PostgrestStore } from "./store.ts";
import { TelegramHttp } from "./telegram.ts";
import { buildVisionRequest, callVision } from "./vision.ts";

function intEnv(name: string, fallback: number, min: number, max: number): number {
  const raw = Deno.env.get(name);
  if (raw === undefined || raw === "") return fallback;
  const n = Number(raw);
  return Number.isInteger(n) && n >= min && n <= max ? n : fallback;
}

function configure(): Deps | null {
  const need = (name: string) => Deno.env.get(name) ?? "";
  const secretToken = need("TELEGRAM_WEBHOOK_SECRET");
  const allowedChatId = need("ALLOWED_CHAT_ID");
  const botToken = need("TELEGRAM_BOT_TOKEN");
  const anthropicKey = need("ANTHROPIC_API_KEY");
  const supabaseUrl = need("SUPABASE_URL");
  const serviceKey = need("SUPABASE_SERVICE_ROLE_KEY");
  const dispatchToken = need("GITHUB_DISPATCH_TOKEN");
  if (![secretToken, allowedChatId, botToken, anthropicKey, supabaseUrl, serviceKey].every((v) => v.length > 0)) return null;
  const repo = need("GITHUB_REPOSITORY") || "14leux/PRORACLE-research";
  return {
    secretToken,
    allowedChatId,
    dailyCap: intEnv("INTAKE_DAILY_CALL_CAP", 10, 1, 100),
    model: need("VISION_MODEL") || "claude-sonnet-5-5",
    utcOffsetMinutes: intEnv("INTAKE_UTC_OFFSET_MINUTES", 180, -720, 840),
    // On unless INTAKE_ANCHOR_MATCHING says off: a missing or odd value never switches matching off.
    anchorMatching: anchorEnabled(Deno.env.get("INTAKE_ANCHOR_MATCHING")),
    store: new PostgrestStore(supabaseUrl, serviceKey),
    telegram: new TelegramHttp(botToken),
    vision: (body: ReturnType<typeof buildVisionRequest>) => callVision(fetch, anthropicKey, body),
    // The narrow token starts only the intake workflow in one repository; without it nothing is computed.
    dispatch: async (intakeId: string) => {
      if (dispatchToken.length === 0) return false;
      try {
        const response = await fetch(`https://api.github.com/repos/${repo}/actions/workflows/ticket_intake.yml/dispatches`, {
          method: "POST",
          headers: {
            authorization: `Bearer ${dispatchToken}`,
            accept: "application/vnd.github+json",
            "x-github-api-version": "2022-11-28",
            "content-type": "application/json",
          },
          body: JSON.stringify({ ref: "main", inputs: { intake_id: intakeId } }),
          signal: AbortSignal.timeout(20_000),
        });
        await response.body?.cancel();
        return response.status === 204;
      } catch {
        return false;
      }
    },
    now: () => new Date(),
  };
}

Deno.serve(async (request: Request) => {
  const deps = configure();
  if (deps === null) return new Response("not configured", { status: 500 });
  return await handleRequest(request, deps);
});
