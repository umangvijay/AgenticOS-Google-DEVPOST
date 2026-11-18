import { NextResponse } from "next/server";
import { readBoundedJson, BodyError } from "@/lib/server-request";
const API = process.env.NEXT_PUBLIC_API_URL || process.env.API_URL || "http://localhost:8000/api/v1";

const DEFAULT_POSTS = [
  {
    title: "Introducing Sandtable: the workspace that builds its own tools",
    date: "August 15, 2026",
    category: "Product",
    excerpt: "Tell Sandtable a goal. It plans, builds missing app integrations, and executes with live data — not a hardcoded catalog.",
  },
  {
    title: "How MCP creation works",
    date: "August 12, 2026",
    category: "Engineering",
    excerpt: "From an OpenAPI spec, docs URL, or a plain-language description, the factory normalizes, probes, and registers tools the agent can call immediately.",
  },
  {
    title: "Bring your own Gemini key",
    date: "August 10, 2026",
    category: "Guides",
    excerpt: "Store a Gemini API key in Vault as `gemini` and Sandtable will use your quota for planning, generation, and MCP builds.",
  },
];

export async function GET() {
  try {
    const res = await fetch(`${API}/blogs`, { cache: "no-store", signal: AbortSignal.timeout(10000) });
    if (!res.ok) return NextResponse.json({ error: "Posts temporarily unavailable" }, { status: 503 });
    const posts = await res.json();
    return NextResponse.json([...posts, ...DEFAULT_POSTS]);
  } catch { return NextResponse.json({ error: "Posts temporarily unavailable" }, { status: 503 }); }
}

export async function POST(request: Request) {
  const auth = request.headers.get("authorization") || "";
  if (!auth.toLowerCase().startsWith("bearer ")) return NextResponse.json({ error: "Authentication required" }, { status: 401 });
  try {
    const body = await readBoundedJson(request);
    const res = await fetch(`${API}/blogs`, {
      method: "POST", headers: { "Content-Type": "application/json", Authorization: auth },
      body: JSON.stringify(body), signal: AbortSignal.timeout(15000),
    });
    const data = await res.json();
    return NextResponse.json(res.ok ? data : { error: data.detail || "Could not publish post" }, { status: res.status });
  } catch (error) {
    return NextResponse.json({ error: "Could not publish post" }, { status: error instanceof BodyError ? error.status : 503 });
  }
}
