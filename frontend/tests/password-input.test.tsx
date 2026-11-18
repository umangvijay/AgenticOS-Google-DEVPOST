import { act } from "react";
import { createRoot } from "react-dom/client";
import { expect, it, vi } from "vitest";
import PasswordInput from "../src/components/PasswordInput";

it("toggles password visibility without changing the value or submitting the form", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  const submit = vi.fn((event: React.FormEvent) => event.preventDefault());
  try {
    await act(async () => root.render(<form onSubmit={submit}><label htmlFor="password">Password</label><PasswordInput id="password" defaultValue="example-value" autoComplete="current-password" /></form>));
    const input = host.querySelector("input")!;
    const button = host.querySelector("button")!;
    expect(input.type).toBe("password");
    await act(async () => button.click());
    expect(input.type).toBe("text");
    expect(input.value).toBe("example-value");
    expect(button.getAttribute("aria-label")).toBe("Hide password");
    expect(submit).not.toHaveBeenCalled();
    await act(async () => button.click());
    expect(input.type).toBe("password");
  } finally {
    await act(async () => root.unmount());
    host.remove();
    vi.unstubAllGlobals();
  }
});
