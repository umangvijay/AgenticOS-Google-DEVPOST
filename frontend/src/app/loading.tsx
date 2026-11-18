export default function Loading() {
  return <main className="route-state" aria-busy="true" aria-label="Loading page">
    <div className="route-state-panel">
      <span className="draft-eyebrow">SANDTABLE / WORKSPACE</span>
      <h1>Opening your next sheet.</h1>
      <p role="status">Loading the page…</p>
      <div className="route-skeleton" aria-hidden="true"><span /><span /><span /></div>
    </div>
  </main>;
}
