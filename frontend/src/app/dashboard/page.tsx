"use client";

import ChatComposer from "@/components/ChatComposer";

export default function DashboardPage() {
  return (
    <div className="workspace-home animate-fade-in-up">
      <div className="workspace-hero">
        <h1 className="workspace-title">Where should we begin?</h1>
        <p className="workspace-sub">
          Ask a question, explore an idea, or get help with your work.
        </p>
      </div>

      <div className="workspace-composer-wrap">
        <ChatComposer />
      </div>

      <details className="mcp-howto">
        <summary>Connect apps and build tools</summary>
        <ol>
          <li>
            <strong>OpenAPI URL.</strong> Paste the spec: “Create MCP tools from https://…/openapi.json then list …”
          </li>
          <li>
            <strong>Any HTTP API (no OpenAPI).</strong> “Build MCP tools for [app] so I can [list / get / create …]”
          </li>
          <li>
            <strong>Website.</strong> Try “Create MCP tools for YouTube Music.” After the build finishes, ask “Use readStartPage to read the page title.”
          </li>
        </ol>
        <p>
          Open the integration to see which actions passed their tests. For an existing website integration, choose Run Test to check its public start page.
        </p>
      </details>
    </div>
  );
}
