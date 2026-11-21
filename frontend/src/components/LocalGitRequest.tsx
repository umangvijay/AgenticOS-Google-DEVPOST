"use client";
export default function LocalGitRequest({ operation, url, branch }: { operation: string; url: string; branch?: string }) {
  if (!["fetch", "pull", "push"].includes(operation)) return null;
  const request = JSON.stringify({ operation, url, branch });
  return <details className="glass-card" style={{ padding: 14 }}>
    <summary>Run Git {operation} on your device</summary>
    <p>Target: <code>{url}</code> · {branch || "current branch"}</p>
    <p>Save the <a href="/sandtable-git.py" download>Git helper</a> and <a
      href={"data:application/json;charset=utf-8," + encodeURIComponent(request)}
      download="sandtable-git-request.json">this request</a> together. From the local checkout, run:</p>
    <code>python3 ~/Downloads/sandtable-git.py ~/Downloads/sandtable-git-request.json</code>
    <p>Use <code>--remote NAME</code> if your remote is not called origin.
      {operation === "push" && " Stage the files or folders to include first."} Review the destination in your terminal to authorize. Git uses your local credentials and prints the result there.</p>
  </details>;
}
