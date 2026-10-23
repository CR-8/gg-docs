// @ts-check
// Static build into dist/, which the portal (FastAPI) serves. `npm run dev` proxies the API to a portal on :7000.
import tailwindcss from "@tailwindcss/vite"
import { defineConfig } from "astro/config"
import react from "@astrojs/react"

const portal = "http://127.0.0.1:7000"

export default defineConfig({
  integrations: [react()],
  vite: {
    plugins: [tailwindcss()],
    server: { proxy: { "/api": portal, "/auth": portal, "/site": portal, "/invite": portal } },
  },
})
