import { vi } from "vitest";

export interface Call {
  method: string;
  url: string;
  body: unknown;
  contentType: string | null;
}

// Replace fetch with a table of canned JSON responses keyed by "METHOD /path".
export function mockFetch(routes: Record<string, unknown>): Call[] {
  const calls: Call[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init: RequestInit = {}) => {
      const method = init.method ?? "GET";
      const headers = (init.headers ?? {}) as Record<string, string>;
      calls.push({
        method,
        url,
        body: init.body ? JSON.parse(String(init.body)) : undefined,
        contentType: headers["Content-Type"] ?? null,
      });
      const path = url.split("?")[0];
      const key = `${method} ${path}`;
      const found = key in routes ? routes[key] : routes[`${method} ${url}`];
      const status = found === undefined ? 404 : 200;
      const body = found === undefined ? { detail: `no mock for ${key}` } : found;
      return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
    }),
  );
  return calls;
}
