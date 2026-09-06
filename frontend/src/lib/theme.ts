/**
 * 主题管理：浅色默认，深色一键切换，持久化到 localStorage。
 * .dark 类挂在 <html> 上，token 见 index.css。
 */

const KEY = "yikou-theme"

export type Theme = "light" | "dark"

export function initialTheme(): Theme {
  try {
    const saved = localStorage.getItem(KEY)
    if (saved === "dark" || saved === "light") return saved
  } catch {
    /* localStorage 不可用时保持浅色 */
  }
  return "light"
}

export function applyTheme(theme: Theme): void {
  document.documentElement.classList.toggle("dark", theme === "dark")
  try {
    localStorage.setItem(KEY, theme)
  } catch {
    /* 忽略持久化失败 */
  }
}
