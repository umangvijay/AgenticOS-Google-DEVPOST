"use client";

import ChatComposer from "@/components/ChatComposer";

export default function DashboardPage() {
  return (
    <div className="workspace-home animate-fade-in-up">
      <div className="workspace-hero">
        <div className="hero-orb" aria-hidden />
        <h1 className="workspace-title">Where should we begin?</h1>
        <p className="workspace-sub">
          Tell Sandtable the outcome you want. It plans the work, can build missing app tools, and runs the workflow against connected services.
        </p>
      </div>

      <div className="workspace-composer-wrap">
        <ChatComposer />
      </div>

      <div className="mcp-howto glass-panel">
        <h2>How to use MCP tools from chat</h2>
        <ol>
          <li>
            <strong>OpenAPI URL.</strong> Paste the spec: “Create MCP tools from https://…/openapi.json then list …”
          </li>
          <li>
            <strong>Any HTTP API (no OpenAPI).</strong> “Build MCP tools for [app] so I can [list / get / create …]”
          </li>
          <li>
            <strong>Website (no API).</strong> Vault → save a short name like <code>mysite-login</code> (username/email + password).
            Chat: “Create MCP tools for https://…”. Then: “Log in with vault credential mysite-login and open home / use runOnSite …”
          </li>
        </ol>
        <p>
          The factory can register HTTP tools from OpenAPI or a REST sketch; websites use origin-locked browser tools. CAPTCHA, OTP, and MFA pause for you — Sandtable does not bypass them.
        </p>
      </div>
    </div>
  );
}
