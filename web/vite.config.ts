/// <reference types="vitest/config" />
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// Port 3000 is the origin the API's CORS settings and the Google sign-in client allow.
export default defineConfig({
  plugins: [react()],
  // Settings come from the repo's .env, shared with the API. Only variables with these prefixes
  // reach the browser: VITE_* and the (public) Google client ID, never the secrets beside them.
  envDir: '..',
  envPrefix: ['VITE_', 'GOOGLE_CLIENT_ID'],
  server: { port: 3000, strictPort: true },
  preview: { port: 3000, strictPort: true },
  test: {
    environment: 'jsdom',
    setupFiles: ['src/test/setup.ts'],
  },
})
