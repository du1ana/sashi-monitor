import { defineConfig } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';
import { viteSingleFile } from 'vite-plugin-singlefile';

export default defineConfig({
  plugins: [svelte(), viteSingleFile()],
  build: {
    target: 'es2020',
    cssCodeSplit: false,
    assetsInlineLimit: 100000000,
    rollupOptions: {
      output: { inlineDynamicImports: true }
    }
  },
  server: {
    proxy: {
      '/api':     'http://127.0.0.1:8765',
      '/healthz': 'http://127.0.0.1:8765'
    }
  }
});
