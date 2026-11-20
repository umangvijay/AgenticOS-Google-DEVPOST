import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.dirname(fileURLToPath(import.meta.url));
const standalone = path.join(root, ".next", "standalone");

function syncDirectory(source, destination) {
  if (!fs.existsSync(source)) return;
  fs.rmSync(destination, { recursive: true, force: true });
  fs.cpSync(source, destination, { recursive: true });
}

if (fs.existsSync(standalone)) {
  // Next traces this repository as a monorepo locally, but Docker builds
  // frontend alone. Copy assets beside the server for THIS build in either layout.
  const buildId = fs.readFileSync(path.join(root, ".next", "BUILD_ID"), "utf8");
  const serverRoot = [standalone, path.join(standalone, "frontend")].find(candidate => {
    const id = path.join(candidate, ".next", "BUILD_ID");
    return fs.existsSync(path.join(candidate, "server.js")) && fs.existsSync(id)
      && fs.readFileSync(id, "utf8") === buildId;
  });
  if (!serverRoot) throw new Error("No standalone server matches the current build");
  syncDirectory(path.join(root, ".next", "static"), path.join(serverRoot, ".next", "static"));
  syncDirectory(path.join(root, "public"), path.join(serverRoot, "public"));
  console.log("Prepared standalone server with static and public assets.");
}
