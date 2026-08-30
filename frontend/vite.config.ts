import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// The build output is copied to /app/static in the runtime image and served by
// FastAPI (Dockerfile, plan.md §9.1). Hashed files land in dist/assets, which
// the backend mounts at /assets.
export default defineConfig({
  plugins: [react()],
  server: {
    // Local dev only: `npm run dev` on 5173, backend on 8080. In production
    // both are the same origin, so no proxy is involved.
    proxy: {
      '/api': 'http://127.0.0.1:8080',
      '/ws': { target: 'ws://127.0.0.1:8080', ws: true },
    },
  },
})
