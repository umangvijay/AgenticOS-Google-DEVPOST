import { expect, it } from "vitest";
import { readBoundedJson } from "../src/lib/server-request";

it("rejects oversized streamed JSON without trusting content-length", async () => {
  const request = new Request("https://example.com", { method: "POST", body: JSON.stringify({ text: "x".repeat(100) }) });
  await expect(readBoundedJson(request, 50)).rejects.toMatchObject({ status: 413 });
});

it("rejects malformed JSON and parses valid JSON", async () => {
  await expect(readBoundedJson(new Request("https://example.com", { method: "POST", body: "{" }))).rejects.toMatchObject({ status: 400 });
  await expect(readBoundedJson(new Request("https://example.com", { method: "POST", body: '{"message":"hello"}' }))).resolves.toEqual({ message: "hello" });
});
