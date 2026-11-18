import { readBoundedJson, BodyError } from "@/lib/server-request";
import { NextResponse } from "next/server";

const TO = process.env.CONTACT_TO_EMAIL || "godumang35@gmail.com";
const API = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000/api/v1";

export async function GET() {
  try {
    const backend = await fetch(`${API}/contact/status`, { cache: "no-store" });
    const data = await backend.json().catch(() => ({}));
    if (!backend.ok) {
      return NextResponse.json({ smtp_configured: false, to: TO, setup: data.setup || "" });
    }
    return NextResponse.json({
      smtp_configured: Boolean(data.smtp_configured),
      to: data.to || TO,
      host: data.host || null,
      setup: data.setup || "",
    });
  } catch {
    return NextResponse.json({ smtp_configured: false, to: TO, setup: "Start the Sandtable backend, then set CONTACT_SMTP_PASSWORD in .env." });
  }
}

export async function POST(request: Request) {
  try {
    const body = await readBoundedJson(request);
    const backend = await fetch(`${API}/contact`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body), signal: AbortSignal.timeout(65000),
    });
    const data = await backend.json().catch(() => ({}));
    return NextResponse.json(backend.ok ? data : { error: data.detail || "Could not send message", saved: false }, { status: backend.status });
  } catch (error) {
    const status = error instanceof BodyError ? error.status : 503;
    return NextResponse.json({ error: status === 503 ? "Could not confirm delivery. Please try again later." : String(error), saved: status === 503 ? null : false }, { status });
  }
}
