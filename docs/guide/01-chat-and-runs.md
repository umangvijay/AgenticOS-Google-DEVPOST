# Chat & Autonomous Runs

The chat is the whole product. Every message becomes a **run**: AgentOS plans a task graph (DAG), picks the right agent for each task (chat, health check, HTTP, MCP builder, orchestrator), and executes it live.

**How to use**
- Type a goal. Press Enter. The run starts immediately.
- Follow-ups stay in the same thread — the agent remembers the conversation. Just keep typing in the same chat.
- **Execution Timeline** under the chat shows every step as it happens (workflow started, tool calls, completions). Click its header to collapse/expand; it auto-collapses when you scroll.
- The **prompt rail** on the far left appears in longer conversations — one pill per message; hover to preview the reply, click to jump to that message.
- Buttons while a run is live: **Stop** cancels it; **Retry** re-runs a failure; **Details** opens the full run page with the DAG graph and complete event stream.

**Nothing is canned.** Replies come from the live model; tool results come from real HTTP calls, real browsers, real scrapes — every failed call carries a machine-readable reason and recovery hint the agent uses to self-correct.
