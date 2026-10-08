/// <reference types="vitest/config" />
import react from '@vitejs/plugin-react'
import { defineConfig, loadEnv, type Plugin } from 'vite'
import { pagesHeaders } from './src/lib/securityHeaders.ts'

// Settings come from the repo's .env, shared with the API. Only variables with these prefixes
// reach the browser: VITE_* and the (public) Google client ID, never the secrets beside them.
const ENV_DIR = '..'
const ENV_PREFIX = ['VITE_', 'GOOGLE_CLIENT_ID']

/** Writes Cloudflare Pages' _headers file (security headers) into the build. */
function pagesHeadersFile(apiBaseUrl: string): Plugin {
  return {
    name: 'cloudflare-pages-headers',
    apply: 'build',
    generateBundle() {
      this.emitFile({
        type: 'asset',
        fileName: '_headers',
        source: pagesHeaders(apiBaseUrl),
      })
    },
  }
}

// Port 3000 is the origin the API's CORS settings and the Google sign-in client allow.
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, ENV_DIR, ENV_PREFIX)
  const apiBaseUrl = env.VITE_API_BASE_URL || 'http://localhost:8000'
  return {
    plugins: [react(), pagesHeadersFile(apiBaseUrl)],
    envDir: ENV_DIR,
    envPrefix: ENV_PREFIX,
    server: { port: 3000, strictPort: true },
    preview: { port: 3000, strictPort: true },
    test: {
      environment: 'jsdom',
      setupFiles: ['src/test/setup.ts'],
    },
  }
})
