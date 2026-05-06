// Copies the single-file build to repo root so the daemon
// (and installer) can serve/deploy it directly.
import { copyFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const src  = resolve(here, '..', 'dist', 'index.html');
const dst  = resolve(here, '..', '..', 'index.html');

copyFileSync(src, dst);
console.log('[postbuild] wrote', dst);
