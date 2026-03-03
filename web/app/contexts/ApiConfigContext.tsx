'use client';

import { createContext, useContext, useEffect, useState, useCallback } from 'react';

const BOOTSTRAP_URL = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8001';

export interface AppSettings {
  api_base_url: string;
  cors_origins: string;
  debug_log_path: string;
  debug_ingest_url: string;
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
      const base = (data.api_base_url || '').trim() || BOOTSTRAP_URL;
      setApiBaseUrlState(base.replace(/\/+$/, ''));
      setAppSettings({
        api_base_url: data.api_base_url ?? '',
        cors_origins: data.cors_origins ?? '',
        debug_log_path: data.debug_log_path ?? '',
        debug_ingest_url: data.debug_ingest_url ?? '',
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
    setAppSettings((prev) => (prev ? { ...prev, api_base_url: base } : null));
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
