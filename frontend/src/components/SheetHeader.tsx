import type { CSSProperties, ReactNode } from "react";

/**
 * Shared public-page header in the home page's drafting language:
 * bracketed console frame, mono eyebrow with glowing dot, serif display
 * title, and a quiet deck. Optionally carries index text (01 / 05).
 */
export default function SheetHeader({
  eyebrow,
  index,
  title,
  deck,
  children,
  style,
}: {
  eyebrow: string;
  index?: string;
  title: ReactNode;
  deck?: ReactNode;
  children?: ReactNode;
  style?: CSSProperties;
}) {
  return (
    <header className="sheet-head" data-scroll-reveal style={style}>
      <span className="frame-tr" aria-hidden />
      <span className="frame-bl" aria-hidden />
      <div className="draft-eyebrow sheet-eyebrow-row">
        <span className="draft-dot" aria-hidden /> {eyebrow}
        {index && <span className="sheet-head-index">{index}</span>}
      </div>
      <h1 className="sheet-title">{title}</h1>
      {deck && <p className="sheet-deck">{deck}</p>}
      <span className="sheet-rule" aria-hidden />
      {children}
    </header>
  );
}
