import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: {
      // Same-origin in development, so the SSE stream needs no CORS
      // preflight and cookies would work unchanged once real auth lands.
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
        // Vite buffers proxied responses by default, which would hold the
        // whole answer until the stream closed and defeat token streaming.
        configure: (proxy) => {
          proxy.on('proxyRes', (proxyRes) => {
            proxyRes.headers['cache-control'] = 'no-cache, no-transform'
          })
        },
      },
    },
  },
})
