'use client';

import { createContext, useContext } from 'react';
import { useRouter } from 'next/navigation';

const NavigationContext = createContext<{ navigateToSettings: () => void } | undefined>(undefined);

export function NavigationProvider({ children }: { children: React.ReactNode }) {
  const router = useRouter();

  const navigateToSettings = () => {
    router.push('/settings');
  };

  return (
    <NavigationContext.Provider value={{ navigateToSettings }}>
      {children}
    </NavigationContext.Provider>
  );
}

export function useNavigation() {
  const ctx = useContext(NavigationContext);
  if (ctx === undefined) {
    throw new Error('useNavigation must be used within NavigationProvider');
  }
  return ctx;
}
