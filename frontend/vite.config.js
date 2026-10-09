import { defineConfig } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';
import { viteSingleFile } from 'vite-plugin-singlefile';

// `npm run dev` proxies the API to a running sashimon (default: the local test monitor on :18765).
const target = process.env.SASHIMON_URL || 'http://127.0.0.1:18765';

export default defineConfig({
  plugins: [svelte(), viteSingleFile()],
  build: { target: 'es2020', cssCodeSplit: false, assetsInlineLimit: 100000000 },
  server: { proxy: { '/api': { target, secure: false }, '/healthz': { target, secure: false } } }
});
