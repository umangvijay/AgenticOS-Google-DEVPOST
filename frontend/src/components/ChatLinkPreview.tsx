"use client";

/**
 * §28.7 — the shared chat link-card interaction.
 *
 * One anchor implementation for BOTH renderers (sent messages and
 * sanitized assistant Markdown). Preserves normal anchor semantics —
 * modifier-click, context menu, middle-click, selection, copy — while
 * adding a NONMODAL preview card on hover intent or keyboard focus.
 *
 * Guarantees:
 * - Hover/focus performs ONE bounded metadata fetch. It never creates a
 *   browser session, model usage, build or tool execution.
 * - Only one preview request is live per component; focusing link B
 *   aborts link A's in-flight request and discards any late response.
 * - Escape dismisses the card and returns focus to the link.
 * - Touch devices get explicit Preview/Open controls instead of hover.
 * - The card clamps/flips inside the viewport.
 */

import {
  useCallback, useEffect, useId, useLayoutEffect, useRef, useState,
} from "react";
import { getLinkPreview, LinkPreview } from "@/lib/api";
import { createPortal } from "react-dom";

const HOVER_INTENT_MS = 160;
const CARD_WIDTH = 288;

export type PreviewAnchorProps = {
  href: string;
  children: React.ReactNode;
  /** Sent messages render plain text (not Markdown) — anchors get the
   * user-bubble link styling. */
  variant?: "user" | "agent";
};

export function isEligibleHttpUrl(href: string): boolean {
  try {
    const parsed = new URL(href, window.location.href);
    return parsed.protocol === "https:" || parsed.protocol === "http:";
  } catch {
    return false;
  }
}

function safeDisplayUrl(href: string): string {
  try {
    const parsed = new URL(href, window.location.href);
    parsed.hash = "";
    if (parsed.username || parsed.password) return `${parsed.host}/…`;
    return `${parsed.host}${parsed.pathname === "/" ? "" : parsed.pathname}`;
  } catch {
    return href;
  }
}

/** Linkify eligible HTTP(S) runs in PLAIN text (sent messages), preserving
 * punctuation, balanced parentheses, line breaks and code spans. */
export function linkifyText(text: string, render: (href: string, content: React.ReactNode, key: string) => React.ReactNode): React.ReactNode[] {
  const out: React.ReactNode[] = [];
  // Split on fenced/inline code spans first — their contents are never
  // linkified.
  const codeSplit = text.split(/(`[^`\n]+`|```[\s\S]*?```)/g);
  codeSplit.forEach((chunk, chunkIndex) => {
    if (chunk.startsWith("`")) {
      out.push(<span key={`code-${chunkIndex}`}>{chunk}</span>);
      return;
    }
    // URL pattern: whitespace- or punctuation-delimited, trailing
    // punctuation excluded, balanced parens kept.
    const urlRe = /https?:\/\/[^\s<>"]+/g;
    let last = 0;
    let match: RegExpExecArray | null;
    let partIndex = 0;
    while ((match = urlRe.exec(chunk)) !== null) {
      const candidate = match[0];
      // Trim trailing punctuation and unbalanced closing parens.
      let trimmed = candidate;
      for (;;) {
        const lastChar = trimmed[trimmed.length - 1];
        if (!lastChar || !(".,;:!?)".includes(lastChar))) break;
        if (lastChar === ")") {
          const opens = (trimmed.match(/\(/g) || []).length;
          const closes = (trimmed.match(/\)/g) || []).length;
          if (closes > opens) {
            trimmed = trimmed.slice(0, -1);
            continue;
          }
        }
        if (lastChar !== ")") {
          trimmed = trimmed.slice(0, -1);
          continue;
        }
        break;
      }
      const start = match.index;
      const end = start + trimmed.length;
      if (start > last) out.push(<span key={`t-${chunkIndex}-${partIndex++}`}>{chunk.slice(last, start)}</span>);
      out.push(render(
        trimmed,
        trimmed.replace(/^https?:\/\//, "").replace(/\/$/, ""),
        `u-${chunkIndex}-${partIndex++}`,
      ));
      last = end;
      urlRe.lastIndex = end;
    }
    if (last < chunk.length) out.push(<span key={`t-${chunkIndex}-${partIndex++}`}>{chunk.slice(last)}</span>);
  });
  return out;
}

export function ChatLinkPreviewAnchor({ href, children, variant = "agent" }: PreviewAnchorProps) {
  const [open, setOpen] = useState(false);
  const [preview, setPreview] = useState<LinkPreview | null>(null);
  const [phase, setPhase] = useState<"idle" | "loading" | "ready" | "error">("idle");
  const [position, setPosition] = useState<{ top: number; left: number } | null>(null);
  const anchorRef = useRef<HTMLAnchorElement>(null);
  const cardRef = useRef<HTMLDivElement>(null);
  const hoverTimer = useRef<number | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  // §31.21: ref-based in-flight marker — a render-closure `phase` can be
  // stale when a synthetic mouseover (auto-scroll under a stationary
  // cursor) fires between setState and re-render, defeating a state
  // guard and re-entering the abort/refetch loop.
  const inFlightRef = useRef(false);
  const intentRef = useRef<"hover" | "focus" | "touch" | null>(null);
  const suppressFocusRef = useRef(false);
  const cardId = useId();

  const cancelInFlight = useCallback(() => {
    if (hoverTimer.current !== null) {
      window.clearTimeout(hoverTimer.current);
      hoverTimer.current = null;
    }
    if (abortRef.current) {
      abortRef.current.abort();
      abortRef.current = null;
    }
    inFlightRef.current = false;
  }, []);

  const fetchPreview = useCallback((reason: "hover" | "focus" | "touch") => {
    // §31.21: a stationary cursor over an anchor in a page that
    // auto-scrolls (run-event updates) receives SYNTHETIC mouseover
    // events each time the content shifts beneath it. A same-destination
    // re-intent while a fetch is already in flight must NOT cancel it —
    // abort/refetch loops would keep the card loading forever. Different
    // destinations (A→B) and settled/error states still supersede.
    if (inFlightRef.current && intentRef.current === reason) {
      return;
    }
    intentRef.current = reason;
    setPhase((prev) => (prev === "ready" && preview ? prev : "loading"));
    setOpen(true);
    cancelInFlight();
    const controller = new AbortController();
    abortRef.current = controller;
    inFlightRef.current = true;
    const settle = (err: unknown, card?: LinkPreview) => {
      inFlightRef.current = false;
      if (controller.signal.aborted || intentRef.current !== reason) return;
      if (card) {
        setPreview(card);
        setPhase("ready");
      } else {
        setPreview(null);
        setPhase("error");
      }
    };
    try {
      Promise.resolve(getLinkPreview(href, { signal: controller.signal }))
        .then((result) => settle(null, result))
        .catch((err) => settle(err));
    } catch (err) {
      settle(err);
    }
  }, [cancelInFlight, href, preview]);

  const dismiss = useCallback((restoreFocus: boolean) => {
    cancelInFlight();
    intentRef.current = null;
    setOpen(false);
    if (restoreFocus && anchorRef.current) {
      // §28.7: restoring focus must NOT immediately re-open via the
      // focus handler — suppress that one synthetic focus event.
      suppressFocusRef.current = true;
      anchorRef.current.focus();
      window.setTimeout(() => { suppressFocusRef.current = false; }, 0);
    }
  }, [cancelInFlight]);

  const openWithIntent = useCallback((reason: "hover" | "focus" | "touch") => {
    cancelInFlight();
    if (reason === "hover") {
      hoverTimer.current = window.setTimeout(() => fetchPreview("hover"), HOVER_INTENT_MS);
    } else {
      fetchPreview(reason);
    }
  }, [cancelInFlight, fetchPreview]);

  const onAnchorEnter = useCallback(() => {
    if (isCoarsePointer()) return;
    openWithIntent("hover");
  }, [openWithIntent]);

  const onAnchorLeave = useCallback(() => {
    if (isCoarsePointer()) return;
    // Pointer travel from link to card must not dismiss — the card's own
    // enter handler cancels this close. ALSO: an auto-scrolling page
    // (run-event updates) shifts content under a stationary cursor and
    // Chrome fires synthetic mouseleave even though the pointer is still
    // within the anchor's new bounds — re-check the anchor's live :hover
    // before dismissing, or the preview card flaps on every update.
    hoverTimer.current = window.setTimeout(() => {
      const stillOnAnchor = anchorRef.current?.matches(":hover") ?? false;
      // The card is PORTALED to <body>: React's synthetic enter/leave
      // across the portal boundary does not always clear this timer, so
      // consult the BROWSER's live hover chain — the authoritative
      // pointer state — instead of React event bookkeeping.
      const hoverChain = typeof document !== "undefined"
        ? Array.from(document.querySelectorAll(":hover"))
        : [];
      const onCard = hoverChain.some(
        (el) => el === cardRef.current || !!cardRef.current?.contains(el));
      if (!stillOnAnchor && !onCard) {
        dismiss(false);
      }
    }, HOVER_INTENT_MS);
  }, [dismiss]);

  const onCardEnter = useCallback(() => {
    if (hoverTimer.current !== null) {
      window.clearTimeout(hoverTimer.current);
      hoverTimer.current = null;
    }
  }, []);

  const onCardLeave = useCallback(() => {
    if (isCoarsePointer()) return;
    // Same synthetic-leave hazard as the anchor: a content swap (metadata
    // settle) or reflow under a stationary pointer fires a transient
    // card mouseleave — re-check live hover before dismissing.
    hoverTimer.current = window.setTimeout(() => {
      if (!cardRef.current?.matches(":hover")) {
        dismiss(false);
      }
    }, HOVER_INTENT_MS);
  }, [dismiss]);

  const onKeyDown = useCallback((event: React.KeyboardEvent) => {
    if (event.key === "Escape") {
      event.stopPropagation();
      dismiss(true);
      return;
    }
    if (event.key === "Tab" && open) {
      // The card is portaled to <body> (position:fixed must escape the
      // chat subtree's transformed ancestors), so natural DOM-order Tab
      // cannot reach it from the anchor. Forward Tab into the card's
      // first focusable — the standard portal-popover keyboard pattern;
      // Escape still returns focus to the anchor from inside the card.
      const first = cardRef.current
        ?.querySelector<HTMLElement>("a[href], button:not([disabled])");
      if (first) {
        event.preventDefault();
        first.focus();
      }
    }
  }, [dismiss, open]);

  const onAnchorFocus = useCallback(() => {
    if (isCoarsePointer() || suppressFocusRef.current) return;
    openWithIntent("focus");
  }, [openWithIntent]);

  // Position the card after it mounts, clamped/flipped inside the viewport.
  useLayoutEffect(() => {
    if (!open || !anchorRef.current) return;
    // Settle-time repositioning must not yank the card out from under a
    // pointer that is already ON it — an auto-scrolling page shifts the
    // anchor between open and metadata-arrival, and the reposition then
    // fires a synthetic card mouseleave that dismisses an open preview.
    if (cardRef.current?.matches(":hover")) return;
    const anchor = anchorRef.current.getBoundingClientRect();
    const viewportWidth = window.innerWidth;
    const viewportHeight = window.innerHeight;
    let left = anchor.left;
    let top = anchor.bottom + 6;
    if (left + CARD_WIDTH > viewportWidth - 8) {
      left = Math.max(8, viewportWidth - CARD_WIDTH - 8);
    }
    const approxHeight = 190;
    if (top + approxHeight > viewportHeight - 8) {
      top = Math.max(8, anchor.top - approxHeight - 6);
    }
    // Position ONCE per open (clamped/flipped). Repositioning on every
    // phase transition follows a layout-shifting anchor and yanks the
    // card out from under a pointer mid-transfer (auto-scrolling pages
    // shift the anchor between open and metadata arrival).
    setPosition({ top, left });
  }, [open]);

  // Dismiss on scroll away / outside click so text selection or a context
  // menu never leaves the destination touched.
  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: PointerEvent) => {
      const target = event.target as Node;
      if (!cardRef.current?.contains(target) && !anchorRef.current?.contains(target)) {
        dismiss(false);
      }
    };
    window.addEventListener("pointerdown", onPointerDown);
    return () => window.removeEventListener("pointerdown", onPointerDown);
  }, [open, dismiss]);

  // Unmount cleanup: no in-flight request outlives the anchor.
  useEffect(() => cancelInFlight, [cancelInFlight]);

  const anchorStyle: React.CSSProperties = {
    color: variant === "user" ? "white" : "var(--accent, #7dd3fc)",
    textDecoration: "underline",
    textUnderlineOffset: 2,
    wordBreak: "break-word",
  };

  const host = preview?.final_host || safeDisplayUrl(href).split("/")[0];
  const showFallback = phase === "ready" && preview && preview.state !== "ready";
  const title = preview?.title || (showFallback ? null : null);

  return (
    <>
      <a
        ref={anchorRef}
        href={href}
        target="_blank"
        rel="noopener noreferrer"
        style={anchorStyle}
        aria-describedby={open ? cardId : undefined}
        aria-expanded={open}
        onMouseEnter={onAnchorEnter}
        onMouseLeave={onAnchorLeave}
        onFocus={onAnchorFocus}
        onBlur={(event) => {
          if (!cardRef.current?.contains(event.relatedTarget as Node)) {
            dismiss(false);
          }
        }}
        onKeyDown={onKeyDown}
        onClick={(event) => {
          // Touch: the first tap opens Preview/Open affordances instead
          // of navigating immediately (double-tap still opens).
          if (isCoarsePointer() && !open) {
            event.preventDefault();
            openWithIntent("touch");
          }
          // Plain clicks keep normal opening behavior (new tab via
          // target=_blank); no preventDefault for mouse/touch-open state.
        }}
        onContextMenu={(event) => event.stopPropagation()}
      >
        {children}
      </a>
      {open && typeof document !== "undefined" && createPortal(
        <div
          ref={cardRef}
          id={cardId}
          role="dialog"
          aria-label={`Link preview for ${host}`}
          style={{
            position: "fixed",
            top: position?.top ?? -9999,
            left: position?.left ?? -9999,
            width: CARD_WIDTH,
            zIndex: 1000,
            background: "var(--bg-card, #101722)",
            border: "1px solid var(--border-primary, #233)",
            borderRadius: 12,
            boxShadow: "0 12px 32px rgba(0,0,0,0.45)",
            padding: 12,
            color: "var(--text-primary, #e5eef7)",
          }}
          onMouseEnter={onCardEnter}
          onMouseLeave={onCardLeave}
          onKeyDown={onKeyDown}
        >
          <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 8 }}>
            {preview?.favicon_url && (
              // eslint-disable-next-line @next/next/no-img-element
              <img
                src={preview.favicon_url}
                alt=""
                width={16}
                height={16}
                style={{ borderRadius: 3, background: "var(--bg-tertiary, #1a2332)" }}
                onError={(e) => { (e.target as HTMLImageElement).style.display = "none"; }}
              />
            )}
            <span style={{ fontSize: 12, color: "var(--text-secondary, #9fb0c0)", flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
              {host}
            </span>
            <a
              href={href}
              target="_blank"
              rel="noopener noreferrer"
              style={{
                fontSize: 12, fontWeight: 600, color: "var(--accent, #7dd3fc)",
                textDecoration: "none", padding: "3px 8px", borderRadius: 6,
                border: "1px solid var(--border-primary, #233)",
              }}
            >
              Open
            </a>
            {isCoarsePointer() && (
              <button
                type="button"
                onClick={() => dismiss(true)}
                aria-label="Close preview"
                style={{ background: "none", border: "none", color: "var(--text-tertiary, #64748b)", fontSize: 14, cursor: "pointer", padding: 2 }}
              >
                ✕
              </button>
            )}
          </div>
          {phase === "loading" && (
            <div role="status" style={{ fontSize: 12, color: "var(--text-tertiary, #64748b)", padding: "18px 0", textAlign: "center" }}>
              Loading preview…
            </div>
          )}
          {phase === "error" && (
            <div style={{ fontSize: 12, color: "var(--text-tertiary, #64748b)", padding: "14px 0" }}>
              Preview unavailable — use Open to visit {host}.
            </div>
          )}
          {phase === "ready" && preview && (
            <>
              {preview.thumbnail_url && (
                // eslint-disable-next-line @next/next/no-img-element
                <img
                  src={preview.thumbnail_url}
                  alt=""
                  style={{
                    width: "100%", height: 110, objectFit: "cover",
                    borderRadius: 8, marginBottom: 8,
                    background: "var(--bg-tertiary, #1a2332)",
                  }}
                  onError={(e) => { (e.target as HTMLImageElement).style.display = "none"; }}
                />
              )}
              <div style={{ fontSize: 13, fontWeight: 600, lineHeight: 1.35, marginBottom: 4, overflow: "hidden", display: "-webkit-box", WebkitLineClamp: 2, WebkitBoxOrient: "vertical" }}>
                {title || (preview.state === "ready" ? safeDisplayUrl(href) : host)}
              </div>
              {preview.description && (
                <div style={{ fontSize: 12, color: "var(--text-secondary, #9fb0c0)", lineHeight: 1.4, overflow: "hidden", display: "-webkit-box", WebkitLineClamp: 2, WebkitBoxOrient: "vertical" }}>
                  {preview.description}
                </div>
              )}
              {preview.state !== "ready" && (
                <div style={{ fontSize: 11, color: "var(--text-tertiary, #64748b)", marginTop: 6 }}>
                  {preview.state === "limited" ? "Limited metadata available" : "Metadata unavailable"}
                </div>
              )}
            </>
          )}
        </div>,
        document.body)}
    </>
  );
}

function isCoarsePointer(): boolean {
  if (typeof window === "undefined" || !window.matchMedia) return false;
  return window.matchMedia("(pointer: coarse)").matches;
}
