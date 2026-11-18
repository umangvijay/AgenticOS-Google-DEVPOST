-- Phase 22 §22.5 (found by the live probe): pg_org_invitations lacked the
-- production contract columns — the shared invitation service stores only
-- the token HASH and an expiry; the pg table stored a raw unique token and
-- had no expires_at. Additive per the ledger rule.
ALTER TABLE pg_org_invitations ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;
ALTER TABLE pg_org_invitations ADD COLUMN IF NOT EXISTS token_hash TEXT;
-- Legacy rows keep working: copy the stored token as its own hash and
-- stamp a 30-day expiry so old pending invitations expire rather than
-- silently living forever.
UPDATE pg_org_invitations
   SET token_hash = COALESCE(token_hash, encode(sha256(token::bytea), 'hex')),
       expires_at = COALESCE(expires_at, now() + interval '30 days')
 WHERE token_hash IS NULL OR expires_at IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_org_invitations_token_hash
  ON pg_org_invitations(token_hash) WHERE token_hash IS NOT NULL;
