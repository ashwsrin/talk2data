'use client';

import { createContext, useContext, useEffect, useState } from 'react';

export type Theme = 'light' | 'dark' | 'redwood-light' | 'redwood-teal-light' | 'redwood-dark' | 'redwood-plum-dark';

const THEME_IDS: Theme[] = ['light', 'dark', 'redwood-light', 'redwood-teal-light', 'redwood-dark', 'redwood-plum-dark'];

interface ThemeContextType {
  theme: Theme;
  setTheme: (theme: Theme) => void;
}

const ThemeContext = createContext<ThemeContextType | undefined>(undefined);

function applyTheme(theme: Theme) {
  if (typeof document === 'undefined') return;
  const html = document.documentElement;
  html.setAttribute('data-theme', theme);
  if (theme === 'dark' || theme === 'redwood-dark' || theme === 'redwood-plum-dark') {
    html.classList.add('dark');
  } else {
    html.classList.remove('dark');
  }
}

export function ThemeProvider({ children }: { children: React.ReactNode }) {
  useEffect(() => {
    applyTheme('redwood-light');
  }, []);

  return (
    <ThemeContext.Provider value={{ theme: 'redwood-light', setTheme: () => { } }}>
      {children}
    </ThemeContext.Provider>
  );
}

export function useTheme() {
  const context = useContext(ThemeContext);
  if (context === undefined) {
    throw new Error('useTheme must be used within a ThemeProvider');
  }
  return context;
}
