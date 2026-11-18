/**
 * Sandtable brand lockup — an isometric "yantra" mark (machine/instrument)
 * plus wordmark. Pure SVG + CSS; scales with font size.
 */
export default function BrandMark({ size = 28, withWordmark = true }: { size?: number; withWordmark?: boolean }) {
  return (
    <span className="brand-mark">
      <svg width={size} height={size} viewBox="0 0 32 32" fill="none" aria-hidden="true">
        {/* outer rotated square (the yantra frame) */}
        <rect x="7" y="7" width="18" height="18" rx="2.5"
          stroke="var(--steel)" strokeWidth="1.6" transform="rotate(45 16 16)"
          style={{ opacity: 0.85 }} />
        {/* inner square, counter-rotated */}
        <rect x="10.5" y="10.5" width="11" height="11" rx="1.8"
          stroke="var(--ember)" strokeWidth="1.6" />
        {/* the core — a molten chip */}
        <rect x="13.4" y="13.4" width="5.2" height="5.2" rx="1.2" fill="var(--forge-grad)" />
        {/* orbit node */}
        <circle cx="25" cy="7" r="1.9" fill="var(--ember-bright)" />
      </svg>
      {withWordmark && (
        <span style={{
          fontWeight: 700,
          letterSpacing: "-0.02em",
          fontSize: size * 0.72,
          background: "var(--forge-grad)",
          WebkitBackgroundClip: "text",
          backgroundClip: "text",
          color: "transparent",
        }}>
          Sandtable
        </span>
      )}
    </span>
  );
}
