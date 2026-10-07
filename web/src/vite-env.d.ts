/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** The API's address; defaults to http://localhost:8000. */
  readonly VITE_API_BASE_URL?: string
  /** The Google OAuth client ID, shared with the API (read from the repo's .env). */
  readonly GOOGLE_CLIENT_ID?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
