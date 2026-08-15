import { defineConfig } from 'vite'

export default defineConfig({
  // Relative asset paths, because the built bundle is served by the gateway
  // under /g2/ -- the always-on process that already holds Tachikoma's brain.
  // With the default absolute base the assets would resolve to /assets/* and
  // 404 anywhere but the site root.
  base: './',
  server: { host: true, port: 5173 },
  build: { target: 'esnext' },
})
