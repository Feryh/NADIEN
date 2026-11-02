import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

const backend = process.env.NADIEN_API_URL ?? 'http://localhost:8000'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': backend,
      '/docs': backend,
      '/openapi.json': backend,
    },
  },
})
