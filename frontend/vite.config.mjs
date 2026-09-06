import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

const target = process.env.GSEARCH_API_TARGET || 'http://127.0.0.1:8765';
const proxy = {
  '^/api/(meta|participants|search|media-asset|timeline-asset)(\\?|$)': {
    target, changeOrigin: true, secure: true, proxyTimeout: 40000,
  },
  '/media/gcores/': {
    target, changeOrigin: true, secure: true, proxyTimeout: 30000,
    rewrite: path => `/api/media-asset?url=${encodeURIComponent(`https://image.gcores.com/${path.slice('/media/gcores/'.length)}`)}`,
  },
};

export default defineConfig({
  build: { outDir: 'dist/client' },
  optimizeDeps: { include: ['react', 'react-dom/client'] },
  server: { host: '127.0.0.1', proxy },
  preview: { host: '127.0.0.1', proxy },
  plugins: [react()],
});
