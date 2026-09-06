import path from 'node:path'
import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'
import { viteSingleFile } from 'vite-plugin-singlefile'

// https://vite.dev/config/
export default defineConfig({
  // pywebview 以 file:// 加载打包产物；WebKit 会拦截 file:// 下的 ES Module，
  // 因此用 singlefile 插件把 JS/CSS 全部内联进 index.html（也便于 onefile 打包）。
  base: './',
  plugins: [react(), tailwindcss(), viteSingleFile()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
})
