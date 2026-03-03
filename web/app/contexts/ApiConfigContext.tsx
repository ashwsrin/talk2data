'use client';

import { createContext, useContext, useEffect, useState, useCallback } from 'react';

const BOOTSTRAP_URL = '';

export interface AppSettings {
  system_prompt: string;
}

interface ApiConfigContextType {
  apiBaseUrl: string;
  setApiBaseUrl: (url: string) => void;
  appSettings: AppSettings | null;
  refreshSettings: () => Promise<void>;
  isLoading: boolean;
}

const ApiConfigContext = createContext<ApiConfigContextType | undefined>(undefined);

export function ApiConfigProvider({ children }: { children: React.ReactNode }) {
  const [apiBaseUrl, setApiBaseUrlState] = useState<string>(BOOTSTRAP_URL);
  const [appSettings, setAppSettings] = useState<AppSettings | null>(null);
  const [isLoading, setIsLoading] = useState(true);

  const refreshSettings = useCallback(async () => {
    try {
      const url = `${BOOTSTRAP_URL}/api/settings`;
      const response = await fetch(url);
      if (!response.ok) return;
      const data = await response.json();
      // Always use relative URLs — Next.js rewrites proxy /api/* to the backend.
      // backend_url is for server-side use only (attachment URLs etc.)
      setApiBaseUrlState(BOOTSTRAP_URL);
      setAppSettings({
        system_prompt: data.system_prompt ?? '',
      });
    } catch {
      setApiBaseUrlState(BOOTSTRAP_URL);
      setAppSettings(null);
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    refreshSettings();
  }, [refreshSettings]);

  const setApiBaseUrl = useCallback((url: string) => {
    const base = (url || '').trim().replace(/\/+$/, '') || BOOTSTRAP_URL;
    setApiBaseUrlState(base);
  }, []);

  return (
    <ApiConfigContext.Provider
      value={{
        apiBaseUrl,
        setApiBaseUrl,
        appSettings,
        refreshSettings,
        isLoading,
      }}
    >
      {children}
    </ApiConfigContext.Provider>
  );
}

export function useApiConfig() {
  const context = useContext(ApiConfigContext);
  if (context === undefined) {
    throw new Error('useApiConfig must be used within an ApiConfigProvider');
  }
  return context;
}
