/**
 * safeSnippet — shared helper for rendering tool output data.
 *
 * Guarantees: returns a string. Never throws on null, undefined,
 * circular, or any other shape. This is the ONE place tool-result
 * data is stringified for display — nowhere else.
 */
export function safeSnippet(value: unknown, maxLen = 800): string {
  if (value == null) return "";
  if (typeof value === "string") return value.slice(0, maxLen);
  try {
    const s = JSON.stringify(value, null, 2);
    if (typeof s !== "string") return "";
    return s.slice(0, maxLen);
  } catch {
    return String(value).slice(0, maxLen);
  }
}
