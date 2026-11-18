"use client";

import Link from "next/link";
import { Sun, Moon } from "lucide-react";

import BrandMark from "@/components/BrandMark";
import { useState, useEffect, useCallback, useRef } from "react";
import { applyTheme, readThemePref, resolveTheme, type ThemePref } from "@/lib/theme";

/** Sandtable theme event — pages with a 3D scene listen and re-skin. */
export function dispatchThemeEvent(resolved: "light" | "dark") {
  window.dispatchEvent(new CustomEvent("sandtable-theme", { detail: resolved }));
}
import { usePathname } from "next/navigation";

export default function PublicNavbar() {
  const [isMobileMenuOpen, setIsMobileMenuOpen] = useState(false);
  const [lampMode, setLampMode] = useState<"light" | "dark">("dark");
  const [hidden, setHidden] = useState(false);
  const pathname = usePathname();
  const menuButton = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!isMobileMenuOpen) return;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const links = () => Array.from(menu.current?.querySelectorAll<HTMLElement>("a[href], button") ?? []);
    links()[0]?.focus();
    const close = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setIsMobileMenuOpen(false);
        menuButton.current?.focus();
      }
      if (event.key === "Tab") {
        const items = [menuButton.current, ...links()].filter(Boolean) as HTMLElement[];
        const first = items[0], last = items[items.length - 1];
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
      }
    };
    const desktop = window.matchMedia("(min-width: 1280px)");
    const resize = () => { if (desktop.matches) setIsMobileMenuOpen(false); };
    desktop.addEventListener("change", resize);
    window.addEventListener("keydown", close);
    return () => {
      document.body.style.overflow = previousOverflow;
      window.removeEventListener("keydown", close);
      desktop.removeEventListener("change", resize);
    };
  }, [isMobileMenuOpen]);

  useEffect(() => {
    // Route changes should close the transient mobile navigation drawer.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setIsMobileMenuOpen(false);
  }, [pathname]);

  useEffect(() => {
    const sync = () => setLampMode(resolveTheme(readThemePref()));
    sync();
    const observer = new MutationObserver(sync);
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    return () => observer.disconnect();
  }, []);

  const toggleLamp = useCallback(() => {
    const pref: ThemePref = lampMode === "dark" ? "light" : "dark";
    const resolved = applyTheme(pref);
    setLampMode(resolved);
    dispatchThemeEvent(resolved);
  }, [lampMode]);

  useEffect(() => {
    const titles: Record<string, string> = {
      "/": "Home · Sandtable",
      "/about": "About · Sandtable",
      "/features": "Features · Sandtable",
      "/integrations": "Integrations · Sandtable",
      "/docs": "Docs · Sandtable",
      "/faq": "FAQs · Sandtable",
      "/blog": "Blog · Sandtable",
      "/contact": "Contact · Sandtable",
      "/pricing": "Pricing · Sandtable",
      "/community": "Community · Sandtable",
      "/privacy": "Privacy · Sandtable",
      "/terms": "Terms · Sandtable",
      "/get-started": "Get started · Sandtable",
      "/login": "Sign in · Sandtable",
      "/signup": "Sign up · Sandtable",
    };
    if (titles[pathname]) document.title = titles[pathname];

  }, [pathname]);

  useEffect(() => {
    let lastY = 0;
    const onScroll = () => {
      const y = window.scrollY;
      setHidden(y > 88 && y > lastY && !isMobileMenuOpen);
      lastY = y;
    };
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, [isMobileMenuOpen]);

  const navLinks = [
    { href: "/about", label: "About Us" },
    { href: "/features", label: "Features" },
    { href: "/integrations", label: "Integrations" },
    { href: "/docs", label: "Docs" },
    { href: "/faq", label: "FAQs" },
    { href: "/blog", label: "Blog" },
    { href: "/contact", label: "Contact" },
  ];

  return (
    <>
      <header className="navbar-public" onFocusCapture={() => setHidden(false)} style={{
        padding: "20px clamp(16px, 5vw, 40px)",
        display: "flex",
        justifyContent: "space-between",
        alignItems: "center",
        borderBottom: "1px solid var(--border-primary)",
        backdropFilter: "blur(24px)",
        WebkitBackdropFilter: "blur(24px)",
        position: "sticky",
        top: 0,
        zIndex: 100,
        transform: hidden ? "translateY(-110%)" : "translateY(0)",
        transition: "transform 0.25s ease, background 0.3s ease, box-shadow 0.3s ease",
        boxShadow: "0 8px 30px rgba(43,42,39,0.04)",
        gap: 16,
      }}>
        <Link href="/" style={{ textDecoration: "none", flexShrink: 0 }} aria-label="Sandtable Home">
          <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
            <BrandMark size={30} withWordmark={false} />
            <span style={{
              fontSize: 21, fontWeight: 700, letterSpacing: "0.02em",
              fontFamily: "var(--font-serif)",
            }} className="gradient-text">
              SANDTABLE.
            </span>
          </div>
        </Link>

        <button
          type="button"
          onClick={toggleLamp}
          aria-label={`Switch to ${lampMode === "dark" ? "North Light (day)" : "Tungsten (night)"}`}
          aria-pressed={lampMode === "dark"}
          title="Draughting Lamp — North Light / Tungsten"
          style={{
            display: "inline-flex", alignItems: "center", gap: 8,
            padding: "8px 13px", borderRadius: "var(--radius-md)",
            border: "1px solid var(--border-primary)",
            background: "var(--bg-glass)", color: "var(--text-secondary)",
            fontFamily: "var(--font-mono)", fontSize: 11, letterSpacing: "0.08em",
            cursor: "pointer", flexShrink: 0,
          }}
        >
          {lampMode === "dark" ? <Moon size={14} /> : <Sun size={14} />}
          <span className="lamp-label">{lampMode === "dark" ? "TUNGSTEN" : "NORTH LIGHT"}</span>
        </button>

        <nav
          className="public-nav-links"
          style={{ display: "flex", gap: 28, fontSize: 14, fontWeight: 600, color: "var(--text-secondary)", flex: 1, justifyContent: "center" }}
        >
          {navLinks.map((link) => (
            <Link
              key={link.href}
              href={link.href}
              aria-current={(pathname === link.href || pathname.startsWith(`${link.href}/`)) ? "page" : undefined}
              className="hover:text-[var(--text-primary)] transition-colors whitespace-nowrap"
              style={{ color: (pathname === link.href || pathname.startsWith(`${link.href}/`)) ? "var(--text-primary)" : "var(--text-secondary)" }}
            >
              {link.label}
            </Link>
          ))}
        </nav>

        <div style={{ display: "flex", alignItems: "center", gap: 10, flexShrink: 0 }}>
          <Link href="/login" className="btn btn-primary whitespace-nowrap">Sign in →</Link>
          <Link href="/get-started" className="btn btn-secondary whitespace-nowrap public-nav-started hide-sm">Get Started</Link>
          <button
            type="button"
            ref={menuButton}
            className="public-nav-hamburger"
            aria-label={isMobileMenuOpen ? "Close menu" : "Open menu"}
            aria-expanded={isMobileMenuOpen}
            aria-controls="public-mobile-menu"
            onClick={() => setIsMobileMenuOpen((v) => !v)}
            style={{ gap: 6, background: "transparent", border: "none", width: 44, height: 44, display: "none", flexDirection: "column", justifyContent: "center", alignItems: "center", cursor: "pointer" }}
          >
            <span style={{ width: 24, height: 2, background: "var(--text-primary)", borderRadius: 2, transform: isMobileMenuOpen ? "translateY(8px) rotate(45deg)" : undefined, transition: "transform 0.2s" }} />
            <span style={{ width: 24, height: 2, background: "var(--text-primary)", borderRadius: 2, opacity: isMobileMenuOpen ? 0 : 1, transition: "opacity 0.2s" }} />
            <span style={{ width: 24, height: 2, background: "var(--text-primary)", borderRadius: 2, transform: isMobileMenuOpen ? "translateY(-8px) rotate(-45deg)" : undefined, transition: "transform 0.2s" }} />
          </button>
        </div>
      </header>

      {isMobileMenuOpen && (
        <div
          ref={menu}
          className="public-nav-overlay"
          id="public-mobile-menu"
          style={{
            position: "fixed",
            inset: 0,
            top: 72,
            zIndex: 90,
            background: "var(--bg-secondary)",
            overflowY: "auto",
            padding: 24,
          }}
        >
          <nav style={{ display: "flex", flexDirection: "column", gap: 24, fontSize: 18, fontWeight: 600 }}>
            {navLinks.map((link) => (
              <Link
                key={link.href}
                href={link.href}
              aria-current={(pathname === link.href || pathname.startsWith(`${link.href}/`)) ? "page" : undefined}
                className="transition-colors border-b border-[var(--border-primary)] pb-4"
                style={{ color: (pathname === link.href || pathname.startsWith(`${link.href}/`)) ? "var(--text-primary)" : "var(--text-secondary)" }}
              >
                {link.label}
              </Link>
            ))}
            <div style={{ display: "flex", flexDirection: "column", gap: 16, marginTop: 16 }}>
              <Link href="/login" className="btn btn-primary" style={{ justifyContent: "center" }}>Sign In</Link>
              <Link href="/get-started" className="btn btn-secondary" style={{ justifyContent: "center" }}>Get Started for Free</Link>
            </div>
          </nav>
        </div>
      )}
    </>
  );
}
