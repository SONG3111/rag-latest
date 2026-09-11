import { fileURLToPath, URL } from 'node:url'

import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    port: 5173,
    // Bind both stacks. Vite's default resolves "localhost" to IPv6 only on Windows,
    // so http://127.0.0.1:5173 is refused while http://localhost:5173 works — a
    // difference that is easy to mistake for the dev server being down.
    host: '127.0.0.1',
    proxy: {
      // Proxying keeps the browser on a single origin, which means SSE works
      // without any CORS preflight negotiation.
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
      // The health endpoint lives outside /api, so it needs its own rule. Without
      // it Vite's SPA fallback answers the request with index.html, and the client
      // tries to parse HTML as JSON and reports a failure for a healthy backend.
      '/health': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
})
