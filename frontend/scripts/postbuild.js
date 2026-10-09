// Copy the single-file build next to sashimon.py, which serves it as the dashboard.
import { copyFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const dst = resolve(here, '..', '..', 'dashboard.html');
copyFileSync(resolve(here, '..', 'dist', 'index.html'), dst);
console.log('[postbuild] wrote', dst);
