/** Light or dark page background, chosen per browser (Sidebar → Tema). The topology canvas and the area map stay
 * dark in both: their node, label and link colours are drawn for a dark ground (see styles.css). */
export type Theme = 'dark' | 'light'

const THEME_KEY = 'nadien.theme'

export function storedTheme(): Theme {
  try {
    return localStorage.getItem(THEME_KEY) === 'light' ? 'light' : 'dark'
  } catch {
    return 'dark'
  }
}

export function applyTheme(theme: Theme): void {
  document.documentElement.dataset.theme = theme
}

export function saveTheme(theme: Theme): void {
  applyTheme(theme)
  try {
    localStorage.setItem(THEME_KEY, theme)
  } catch {
    /* storage blocked: the choice holds until the page is reloaded */
  }
}
