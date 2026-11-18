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
  syncDirectory(path.join(root, ".next", "static"), path.join(standalone, ".next", "static"));
  syncDirectory(path.join(root, "public"), path.join(standalone, "public"));
  console.log("Prepared standalone server with static and public assets.");
}
