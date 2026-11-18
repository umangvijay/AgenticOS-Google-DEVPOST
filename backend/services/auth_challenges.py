"""Detect CAPTCHA / OTP / MFA pages. AgentOS never solves these — it pauses for a human."""

from __future__ import annotations

import re

from typing import Any, Dict, Optional



class ChallengePause(Exception):
    """Raised when the browser hit a human-only auth challenge. Do not fill or bypass it."""

    def __init__(self, challenge_type: str, url: str, message: str):
        super().__init__(message)
        self.challenge_type = challenge_type
        self.url = url
        self.message = message


def classify_auth_challenge(snapshot: Dict[str, Any]) -> Optional[str]:
    """Return 'captcha' | 'otp' | 'mfa' when the page is a human verification step."""
    text = str(snapshot.get("text") or "").lower()
    title = str(snapshot.get("title") or "").lower()
    blob = f"{title}\n{text}"
    iframes = " ".join(str(x) for x in (snapshot.get("iframes") or [])).lower()
    if any(marker in iframes for marker in ("/recaptcha/", "hcaptcha.com", "challenges.cloudflare.com")):
        return "captcha"
    if any(hint in blob for hint in (
        "verify you are human", "i'm not a robot", "complete the security check",
        "checking your browser", "complete the captcha", "solve the captcha",
    )):
        return "captcha"
    fields = [el for el in (snapshot.get("elements") or []) if isinstance(el, dict)
              and el.get("tag", "input") in ("input", "textarea")]
    labels = " ".join(str(el.get(k) or "") for el in fields
                      for k in ("name", "placeholder", "aria", "autocomplete")).lower()
    # Word boundaries avoid e.g. getRandomFact, /mfa-docs, and otp inside words.
    mfa = re.compile(r"\b(?:mfa|2fa|totp|two[- ]factor|multi[- ]factor|authenticator(?: app)?)\b")
    otp = re.compile(r"\b(?:otp|one[- ]time(?: password| code)?|verification code|6[- ]digit(?: code)?)\b")
    instruction = bool(re.search(r"\b(?:enter|type|provide|input|verify|confirm)\b", blob))
    if mfa.search(labels) or (mfa.search(blob) and instruction and (fields or "enter the code" in blob)):
        return "mfa"
    if otp.search(labels) or (otp.search(blob) and instruction):
        return "otp"
    return None


def challenge_user_message(challenge_type: str, url: str) -> str:
    label = {"captcha": "CAPTCHA", "otp": "one-time passcode (OTP)", "mfa": "multi-factor authentication"}.get(
        challenge_type, "security check"
    )
    return (
        f"AgentOS paused: this page needs you to complete a {label}. "
        "Finish it in the open browser window, then click Resume in the workspace. "
        f"AgentOS will not fill or bypass {label}. Current URL: {url}"
    )
