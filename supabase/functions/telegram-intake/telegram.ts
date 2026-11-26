// Minimal Telegram Bot API client: send a plain-text reply and download a file into memory.
// Replies are plain text (no parse mode), so nothing read from a screenshot can become markup.

import type { FetchLike } from "./vision.ts";

export interface TelegramApi {
  send(chatId: string, text: string): Promise<void>;
  /** The file's bytes, or null when it is larger than `maxBytes`. */
  download(fileId: string, maxBytes: number): Promise<Uint8Array | null>;
}

export class TelegramError extends Error {
  constructor() {
    super("telegram request failed");
  }
}

export const MAX_MESSAGE_CHARS = 4000;

export class TelegramHttp implements TelegramApi {
  constructor(private readonly token: string, private readonly fetchFn: FetchLike = fetch) {}

  private async call(path: string, body: unknown): Promise<unknown> {
    let response: Response;
    try {
      response = await this.fetchFn(`https://api.telegram.org/bot${this.token}/${path}`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(body),
        signal: AbortSignal.timeout(20_000),
      });
    } catch {
      throw new TelegramError();
    }
    if (!response.ok) {
      await response.body?.cancel();
      throw new TelegramError();
    }
    return await response.json();
  }

  async send(chatId: string, text: string): Promise<void> {
    const clipped = text.length > MAX_MESSAGE_CHARS ? text.slice(0, MAX_MESSAGE_CHARS - 1) + "…" : text;
    await this.call("sendMessage", { chat_id: chatId, text: clipped, disable_web_page_preview: true });
  }

  async download(fileId: string, maxBytes: number): Promise<Uint8Array | null> {
    const info = (await this.call("getFile", { file_id: fileId })) as {
      ok?: boolean;
      result?: { file_path?: string; file_size?: number };
    };
    const path = info.result?.file_path;
    if (!info.ok || typeof path !== "string") throw new TelegramError();
    if (typeof info.result?.file_size === "number" && info.result.file_size > maxBytes) return null;
    let response: Response;
    try {
      response = await this.fetchFn(`https://api.telegram.org/file/bot${this.token}/${path}`, {
        signal: AbortSignal.timeout(60_000),
      });
    } catch {
      throw new TelegramError();
    }
    if (!response.ok) {
      await response.body?.cancel();
      throw new TelegramError();
    }
    const declared = Number(response.headers.get("content-length") ?? "0");
    if (declared > maxBytes) {
      await response.body?.cancel();
      return null;
    }
    const bytes = new Uint8Array(await response.arrayBuffer());
    return bytes.length > maxBytes ? null : bytes;
  }
}
