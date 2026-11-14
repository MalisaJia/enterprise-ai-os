import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The built SPA is served same-origin at /workbench/ by the bridge's Node
// process (e.g. http://<your-server-host>:28080/workbench/). All API calls go
// to the relative /workbench/api prefix, so no absolute URLs are baked in.
export default defineConfig({
  base: '/workbench/',
  plugins: [react()],
  build: {
    target: 'esnext',
    cssCodeSplit: false,
    rollupOptions: {
      output: {
        manualChunks: {
          // React runtime in its own chunk (loaded on every page).
          'vendor-react': ['react', 'react-dom'],
          // mammoth is only pulled in on .docx preview — force an isolated
          // chunk so it never leaks into the main bundle.
          mammoth: ['mammoth'],
        },
      },
    },
  },
  server: {
    // Dev mode: same origin layout as production. The API proxy can point at
    // any reachable bridge instance; override with WORKBENCH_API_TARGET.
    // The real deployment address must be supplied via WORKBENCH_API_TARGET --
    // never hardcode a production host in source. Defaults to loopback.
    proxy: {
      '/workbench/api': {
        target: process.env.WORKBENCH_API_TARGET || 'http://127.0.0.1:28080',
        changeOrigin: true,
      },
    },
  },
})
