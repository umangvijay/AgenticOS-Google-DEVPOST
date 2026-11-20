import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
const root = path.dirname(fileURLToPath(import.meta.url));
const buildId = fs.readFileSync(path.join(root, '.next', 'BUILD_ID'), 'utf8');
const serverRoot = ['.next/standalone', '.next/standalone/frontend'].map(p => path.join(root, p)).find(p => {
  const id = path.join(p, '.next', 'BUILD_ID');
  return fs.existsSync(path.join(p, 'server.js')) && fs.existsSync(id) && fs.readFileSync(id, 'utf8') === buildId;
});
if (!serverRoot) throw new Error('Build the frontend before starting the standalone server.');
await import(pathToFileURL(path.join(serverRoot, 'server.js')).href);
