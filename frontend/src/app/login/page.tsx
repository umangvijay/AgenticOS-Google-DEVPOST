"use client";

import AuthFrame from "@/components/AuthFrame";
import PasswordInput from "@/components/PasswordInput";
import { useState, FormEvent, useEffect } from "react";
import { useAuth } from "@/lib/auth-context";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useGoogleLogin, GoogleOAuthProvider } from "@react-oauth/google";

const GOOGLE_CLIENT_ID = process.env.NEXT_PUBLIC_GOOGLE_CLIENT_ID || "";
const GOOGLE_ENABLED = Boolean(GOOGLE_CLIENT_ID) && GOOGLE_CLIENT_ID !== "dummy-client-id";

function GoogleLoginButton({ disabled }: { disabled: boolean }) {
  const { loginWithGoogle } = useAuth();
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const [googleError, setGoogleError] = useState("");

  const googleLogin = useGoogleLogin({
    onSuccess: async (codeResponse) => {
      try {
        setBusy(true);
        await loginWithGoogle(undefined, codeResponse.code);
        router.push("/dashboard");
      } catch (error: unknown) {
        setGoogleError(error instanceof Error ? error.message : "Google sign-in failed. Please try again.");
        setBusy(false);
      }
    },
    onError: () => setGoogleError("Google sign-in couldn’t complete. Please try again."),
    flow: "auth-code",
  });

  return (
    <>
    {googleError && <p className="auth-provider-error" role="alert">{googleError}</p>}
    <button type="button" className="btn btn-secondary btn-lg" style={{ width: "100%" }} onClick={() => { setGoogleError(""); googleLogin(); }} disabled={disabled || busy}>
      <svg width="18" height="18" viewBox="0 0 48 48"><path fill="#EA4335" d="M24 9.5c3.54 0 6.71 1.22 9.21 3.6l6.85-6.85C35.9 2.38 30.47 0 24 0 14.62 0 6.51 5.38 2.56 13.22l7.98 6.19C12.43 13.72 17.74 9.5 24 9.5z"/><path fill="#4285F4" d="M46.98 24.55c0-1.57-.15-3.09-.38-4.55H24v9.02h12.94c-.58 2.96-2.26 5.48-4.78 7.18l7.73 6c4.51-4.18 7.09-10.36 7.09-17.65z"/><path fill="#FBBC05" d="M10.53 28.59c-.48-1.45-.76-2.99-.76-4.59s.27-3.14.76-4.59l-7.98-6.19C.92 16.46 0 20.12 0 24c0 3.88.92 7.54 2.56 10.78l7.97-6.19z"/><path fill="#34A853" d="M24 48c6.48 0 11.93-2.13 15.89-5.81l-7.73-6c-2.15 1.45-4.92 2.3-8.16 2.3-6.26 0-11.57-4.22-13.47-9.91l-7.98 6.19C6.51 42.62 14.62 48 24 48z"/></svg>
      {busy ? "Signing in…" : "Sign in with Google"}
    </button>
    </>
  );
}

function LoginContent() {
  const { login, isAuthenticated, isLoading, user } = useAuth();
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const isGuest = user?.role === "guest";

  useEffect(() => {
    document.title = "Sign in · Sandtable";
  }, []);

  useEffect(() => {
    if (!isLoading && isAuthenticated && !isGuest) router.replace("/dashboard");
  }, [isAuthenticated, isLoading, isGuest, router]);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setError("");
    setLoading(true);
    try {
      await login(email, password);
      router.push("/dashboard");
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Login failed");
    } finally {
      setLoading(false);
    }
  }

  return (
    <AuthFrame title="Welcome back." description={isGuest ? "Sign in to keep this guest workspace." : "Sign in to continue your work."}>
        <div className="auth-form-card">
          <form onSubmit={handleSubmit} aria-busy={loading}>
            {error && (
              <div role="alert" style={{ padding: "10px 14px", marginBottom: 16, background: "var(--error-subtle)", borderRadius: "var(--radius-md)", color: "var(--error)", fontSize: 13 }}>
                {error}
              </div>
            )}
            <div style={{ marginBottom: 16 }}>
              <label htmlFor="login-email" style={{ display: "block", fontSize: 13, fontWeight: 500, color: "var(--text-secondary)", marginBottom: 6 }}>Email</label>
              <input id="login-email" name="email" autoComplete="email" disabled={loading} type="email" className="input" placeholder="you@example.com" value={email} onChange={(e) => setEmail(e.target.value)} required autoFocus />
            </div>
            <div style={{ marginBottom: 24 }}>
              <label htmlFor="login-password" style={{ display: "block", fontSize: 13, fontWeight: 500, color: "var(--text-secondary)", marginBottom: 6 }}>Password</label>
              <PasswordInput id="login-password" name="password" disabled={loading} className="input" autoComplete="current-password" placeholder="Enter your password" value={password} onChange={(e) => setPassword(e.target.value)} required />
            </div>
            <button type="submit" className="btn btn-primary btn-lg" style={{ width: "100%", marginBottom: 16 }} disabled={loading}>
              {loading ? <><span className="spinner" aria-hidden="true" /> Signing in…</> : "Sign In"}
            </button>
          </form>

          <div style={{ display: "flex", alignItems: "center", gap: 12, margin: "16px 0", color: "var(--text-tertiary)", fontSize: 12 }}>
            <div style={{ flex: 1, height: 1, background: "var(--border-primary)" }} /> OR <div style={{ flex: 1, height: 1, background: "var(--border-primary)" }} />
          </div>

          {GOOGLE_ENABLED && <GoogleLoginButton disabled={loading} />}
          <Link href="/signup" className="btn btn-secondary btn-lg" style={{ width: "100%", marginTop: 12 }}>Create an account</Link>
        </div>
                <div style={{ display: "flex", justifyContent: "center", marginTop: 20 }}>
          <span className="trust-badge">
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2"><path d="M12 2l8 4v6c0 5-3.5 8.5-8 10-4.5-1.5-8-5-8-10V6l8-4z" /></svg>
            Protected by industry-standard AES-256 and end-to-end client-side encryption
          </span>
        </div>
        <p style={{ textAlign: "center", marginTop: 24, fontSize: 14, color: "var(--text-secondary)" }}>Need help? <Link href="/contact" style={{ color: "var(--accent)" }}>Contact Support</Link></p>
    </AuthFrame>
  );
}

export default function LoginPage() {
  if (GOOGLE_ENABLED) {
    return (
      <GoogleOAuthProvider clientId={GOOGLE_CLIENT_ID}>
        <LoginContent />
      </GoogleOAuthProvider>
    );
  }
  return <LoginContent />;
}
