import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/ws': {
        target: 'ws://localhost:8000',
        ws: true,
      },
      '/slots': 'http://localhost:8000',
      '/bookings': 'http://localhost:8000',
      '/sessions': 'http://localhost:8000',
      '/audio': 'http://localhost:8000',
    },
  },
})
