/**
 * e2ee.ts — Client-side end-to-end encryption for sensitive values.
 *
 * Credentials are encrypted in the browser with AES-256-GCM BEFORE they ever
 * touch the network. The wrapping key is derived with PBKDF2-SHA256
 * (210,000 iterations) from the user's passphrase + a per-ciphertext random
 * salt; ciphertexts are self-describing JSON envelopes.
 *
 * Threat model: protects secret values against passive network interception
 * and server-side log/storage exposure of transit payloads. The server still
 * re-encrypts at rest with its own AES-256-GCM vault keyed from Secret
 * Manager — the two layers are independent.
 */

const PBKDF2_ITERATIONS = 210_000;
const envelope = { v: 1, alg: "A256GCM", kdf: "PBKDF2-SHA256" } as const;

function b64(buf: ArrayBuffer | Uint8Array): string {
  const bytes = buf instanceof Uint8Array ? buf : new Uint8Array(buf);
  let s = "";
  for (const b of bytes) s += String.fromCharCode(b);
  return btoa(s);
}

function unb64(text: string): Uint8Array {
  const s = atob(text);
  const out = new Uint8Array(s.length);
  for (let i = 0; i < s.length; i++) out[i] = s.charCodeAt(i);
  return out;
}

async function deriveKey(passphrase: string, salt: Uint8Array): Promise<CryptoKey> {
  const base = await crypto.subtle.importKey(
    "raw", new TextEncoder().encode(passphrase), "PBKDF2", false, ["deriveKey"],
  );
  return crypto.subtle.deriveKey(
    { name: "PBKDF2", salt: salt as unknown as BufferSource, iterations: PBKDF2_ITERATIONS, hash: "SHA-256" },
    base,
    { name: "AES-GCM", length: 256 },
    false,
    ["encrypt", "decrypt"],
  );
}

/** Encrypt plaintext into a portable envelope string. */
export async function e2eeEncrypt(plaintext: string, passphrase: string): Promise<string> {
  if (!plaintext || !passphrase) throw new Error("e2ee: plaintext and passphrase are required");
  const salt = crypto.getRandomValues(new Uint8Array(16));
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const key = await deriveKey(passphrase, salt);
  const ciphertext = await crypto.subtle.encrypt(
    { name: "AES-GCM", iv: iv as unknown as BufferSource },
    key,
    new TextEncoder().encode(plaintext),
  );
  return JSON.stringify({ ...envelope, salt: b64(salt), iv: b64(iv), data: b64(ciphertext) });
}

/** Decrypt an envelope produced by e2eeEncrypt. Throws on wrong passphrase/tamper. */
export async function e2eeDecrypt(envelopeJson: string, passphrase: string): Promise<string> {
  const env = JSON.parse(envelopeJson) as { v: number; salt: string; iv: string; data: string };
  if (!env?.data || !env?.salt || !env?.iv) throw new Error("e2ee: not an encrypted envelope");
  const key = await deriveKey(passphrase, unb64(env.salt));
  const plaintext = await crypto.subtle.decrypt(
    { name: "AES-GCM", iv: unb64(env.iv) as unknown as BufferSource },
    key,
    unb64(env.data) as unknown as BufferSource,
  );
  return new TextDecoder().decode(plaintext);
}

/**
 * Generate a strong random passphrase when the user has none (device-local
 * key). Store it in localStorage — this ties E2EE to this browser.
 */
export function ensureLocalPassphrase(): string {
  const KEY = "agentos_e2ee_key";
  let v = "";
  try { v = localStorage.getItem(KEY) || ""; } catch { /* private mode */ }
  if (v) return v;
  const bytes = crypto.getRandomValues(new Uint8Array(32));
  v = b64(bytes);
  try { localStorage.setItem(KEY, v); } catch { /* ignore */ }
  return v;
}
