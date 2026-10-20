// 주간 · 야간 모드. 고른 값은 이 브라우저에만 남는다(localStorage). 저장값이 없으면 주간이다.

export type Theme = 'light' | 'dark';

export const THEME_STORAGE_KEY = 'vigilantis-theme';

/**
 * 첫 화면을 그리기 전에 html에 dark 클래스를 붙인다. React가 붙기 전에 돌아야 주간 화면이
 * 잠깐 보였다가 야간으로 바뀌는 깜빡임이 없다. 저장소를 못 읽으면(사생활 보호 모드 등) 주간으로 둔다.
 */
export const THEME_INIT_SCRIPT = `try{if(localStorage.getItem('${THEME_STORAGE_KEY}')==='dark')document.documentElement.classList.add('dark')}catch(e){}`;

export function readTheme(): Theme {
  return document.documentElement.classList.contains('dark') ? 'dark' : 'light';
}

export function applyTheme(theme: Theme): void {
  document.documentElement.classList.toggle('dark', theme === 'dark');
  try {
    localStorage.setItem(THEME_STORAGE_KEY, theme);
  } catch {
    // 저장하지 못해도 이번 화면에는 적용된다.
  }
}
