import { defineConfig } from 'vite'

export default defineConfig({
  server: {
    proxy: {
      '/api': { target: process.env.VITE_PROXY_TARGET || 'http://127.0.0.1:8000', ws: true },
    },
  },
})
