import { act } from "react";
import { createRoot } from "react-dom/client";
import { expect, it, vi } from "vitest";
import ModelSelector from "../src/components/ModelSelector";

it("keeps exact model IDs, disables unavailable choices, and explains stale selections", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  const onChange = vi.fn();
  const catalog = { provider: "vertex", default_model: "gemini-3.8-flash", documentation_checked: "2026-11-19",
    auth_mode: "service_account_adc", models: [
      { id: "gemini-4-argon", name: "Gemini 4 Argon", family: "Gemini 4", lifecycle: "ga", selectable: true,
        access: "unverified", reason: "", note: "", retirement_date: "", documentation: "" },
      { id: "gemini-3.6-flash", name: "Gemini 3.6 Flash", family: "Retired", lifecycle: "ga", selectable: false,
        access: "unverified", reason: "Retired", note: "", retirement_date: "2026-11-19", documentation: "" },
    ] };
  try {
    await act(async () => root.render(<ModelSelector value="old-model" onChange={onChange} models={[]} catalog={catalog} label="Model" />));
    const select = host.querySelector("select")!;
    expect(select.value).toBe("old-model");
    expect(host.textContent).toContain("no longer enabled");
    expect(host.querySelector<HTMLOptionElement>('option[value="gemini-3.6-flash"]')!.disabled).toBe(true);
    await act(async () => {
      select.value = "gemini-4-argon";
      select.dispatchEvent(new Event("change", { bubbles: true }));
    });
    expect(onChange).toHaveBeenCalledWith("gemini-4-argon");
  } finally {
    await act(async () => root.unmount());
    host.remove();
    vi.unstubAllGlobals();
  }
});
