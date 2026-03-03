'use client';

import { useState, useEffect, useCallback } from 'react';
import Link from 'next/link';
import ReactMarkdown from 'react-markdown';
import { useApiConfig } from '../contexts/ApiConfigContext';

interface Server {
  id: number;
  name: string;
  transport_type: 'sse' | 'streamable_http';
  url: string | null;
  api_key: string | null;
  command: string | null;
  args: string | null;
  env: string | null;
  cwd: string | null;
  is_active: boolean;

  include_in_llm?: boolean;
  system_instruction?: string | null;
  oauth2_access_token_url?: string | null;
  oauth2_client_id?: string | null;
  oauth2_scope?: string | null;
}

interface Tool {
  name: string;
  description: string;
  inputSchema: any;
  full_name?: string;
  enabled?: boolean;
  original_description?: string;
  server_include_in_llm?: boolean;
  effective_enabled?: boolean;
  primary_action_label?: string;
  secondary_action_label?: string;
}

type SettingsTab = 'application' | 'mcp-servers';

interface ModelConfigRow {
  id?: number;
  model_id: string;
  enabled: boolean;
  text_input: boolean;
  image_input: boolean;
  pdf_input: boolean;
  web_search: boolean;
}

export default function SettingsPage() {
  const { apiBaseUrl, setApiBaseUrl, refreshSettings } = useApiConfig();
  const [activeTab, setActiveTab] = useState<SettingsTab>('application');
  const [appSettingsForm, setAppSettingsForm] = useState({
    api_base_url: '',
    cors_origins: '',
    debug_log_path: '',
    debug_ingest_url: '',
    database_url: '',
    oci_config_file: '',
    oci_profile: '',
    // New Oracle DB fields
    oracle_db_dsn: '',
    oracle_db_user: '',
    oracle_wallet_path: '',
    // Custom system prompt
    system_prompt: '',
  });
  const [appSettingsLoading, setAppSettingsLoading] = useState(false);
  const [appSettingsSaving, setAppSettingsSaving] = useState(false);
  const [appSettingsError, setAppSettingsError] = useState<string | null>(null);
  const [appSettingsSuccess, setAppSettingsSuccess] = useState<string | null>(null);
  const [servers, setServers] = useState<Server[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [formData, setFormData] = useState({
    name: '',
    transport_type: 'sse' as 'sse' | 'streamable_http',
    url: '',
    api_key: '',
    command: '',
    args: '',
    env: '',
    cwd: '',

    system_instruction: '',
    oauth2_access_token_url: '',
    oauth2_client_id: '',
    oauth2_client_secret: '',
    oauth2_scope: '',
  });
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const [serverTools, setServerTools] = useState<Record<string, Tool[]>>({});
  const [expandedServers, setExpandedServers] = useState<Set<number>>(new Set());
  const [isAddFormExpanded, setIsAddFormExpanded] = useState(false);
  const [isOAuthExpanded, setIsOAuthExpanded] = useState(false);
  const [expandedToolDetails, setExpandedToolDetails] = useState<Set<string>>(new Set());



  // Fetch servers, tools, and app settings on mount — all in parallel
  useEffect(() => {
    Promise.allSettled([fetchServers(), fetchServerTools(), loadAppSettings()]);
  }, [apiBaseUrl]);

  const loadAppSettings = useCallback(async () => {
    setAppSettingsLoading(true);
    setAppSettingsError(null);
    try {
      const response = await fetch(`${apiBaseUrl}/api/settings`);
      if (!response.ok) throw new Error('Failed to load settings');
      const data = await response.json();
      setAppSettingsForm({
        api_base_url: data.api_base_url ?? '',
        cors_origins: data.cors_origins ?? '',
        debug_log_path: data.debug_log_path ?? '',
        debug_ingest_url: data.debug_ingest_url ?? '',
        database_url: data.database_url ?? '',
        oci_config_file: data.oci_config_file ?? '',
        oci_profile: data.oci_profile ?? '',
        // New fields
        oracle_db_dsn: data.oracle_db_dsn ?? '',
        oracle_db_user: data.oracle_db_user ?? '',
        oracle_wallet_path: data.oracle_wallet_path ?? '',
        system_prompt: data.system_prompt ?? '',
      });
    } catch (err) {
      setAppSettingsError(isNetworkError(err) || (err instanceof Error && err.name === 'AbortError')
        ? `Cannot connect to backend at ${apiBaseUrl}. Make sure the backend is running.`
        : (err instanceof Error ? err.message : 'Failed to load settings'));
    } finally {
      setAppSettingsLoading(false);
    }
  }, [apiBaseUrl]);



  const saveAppSettings = async (e: React.FormEvent) => {
    e.preventDefault();
    setAppSettingsSaving(true);
    setAppSettingsError(null);
    setAppSettingsSuccess(null);
    const url = (appSettingsForm.api_base_url || '').trim().replace(/\/+$/, '');
    if (!url || (!url.startsWith('http://') && !url.startsWith('https://'))) {
      setAppSettingsError('API base URL must be a non-empty http(s) URL.');
      setAppSettingsSaving(false);
      return;
    }
    try {
      const response = await fetch(`${apiBaseUrl}/api/settings`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          api_base_url: url,
          cors_origins: (appSettingsForm.cors_origins || '').trim(),
          debug_log_path: (appSettingsForm.debug_log_path || '').trim(),
          debug_ingest_url: (appSettingsForm.debug_ingest_url || '').trim(),
          // New fields
          oracle_db_dsn: (appSettingsForm.oracle_db_dsn || '').trim(),
          oracle_db_user: (appSettingsForm.oracle_db_user || '').trim(),
          oracle_wallet_path: (appSettingsForm.oracle_wallet_path || '').trim(),
          system_prompt: (appSettingsForm.system_prompt || '').trim(),
        }),
      });
      if (!response.ok) {
        const err = await response.json().catch(() => ({}));
        throw new Error((err as any).detail || response.statusText || 'Failed to save settings');
      }
      const data = await response.json();
      setApiBaseUrl(data.api_base_url ?? url);
      await refreshSettings();
      setAppSettingsSuccess('Settings saved. API base URL is updated for this session.');
    } catch (err) {
      setAppSettingsError(err instanceof Error ? err.message : 'Failed to save settings');
    } finally {
      setAppSettingsSaving(false);
    }
  };

  const fetchServerTools = async () => {
    try {
      const controller = new AbortController();
      const timeoutId = setTimeout(() => controller.abort(), 5000);

      const response = await fetch(`${apiBaseUrl}/api/servers/tools`, {
        signal: controller.signal,
      });
      clearTimeout(timeoutId);

      if (response.ok) {
        const data = await response.json();
        setServerTools(data.server_tools || {});
      }
    } catch (err) {
      // Silently fail - tools might not be loaded yet
      console.log('Could not fetch server tools:', err);
    }
  };

  const toggleToolDetailsExpanded = (fullName: string) => {
    setExpandedToolDetails((prev) => {
      const next = new Set(prev);
      if (next.has(fullName)) next.delete(fullName);
      else next.add(fullName);
      return next;
    });
  };

  const toggleServerExpanded = (serverId: number) => {
    const newExpanded = new Set(expandedServers);
    if (newExpanded.has(serverId)) {
      newExpanded.delete(serverId);
    } else {
      newExpanded.add(serverId);
    }
    setExpandedServers(newExpanded);
  };

  const isNetworkError = (error: unknown): boolean => {
    if (error instanceof TypeError && error.message.includes('Failed to fetch')) {
      return true;
    }
    if (error instanceof Error && (
      error.message.includes('NetworkError') ||
      error.message.includes('Failed to fetch') ||
      error.message.includes('Network request failed')
    )) {
      return true;
    }
    return false;
  };

  const fetchServers = async () => {
    try {
      setLoading(true);
      setError(null);
      const controller = new AbortController();
      const timeoutId = setTimeout(() => controller.abort(), 5000); // 5 second timeout

      const response = await fetch(`${apiBaseUrl}/api/servers`, {
        signal: controller.signal,
      });
      clearTimeout(timeoutId);
      if (!response.ok) {
        const errorText = await response.text();
        throw new Error(`Failed to fetch servers: ${response.status} ${response.statusText}. ${errorText}`);
      }
      const data = await response.json();
      setServers(data);
      setError(null); // Clear any previous errors on success
    } catch (err) {
      if (isNetworkError(err) || (err instanceof Error && err.name === 'AbortError')) {
        setError(`Cannot connect to backend at ${apiBaseUrl}. Make sure the backend is running (e.g. uvicorn app.main:app --reload --port 8001).`);
      } else {
        const errorMessage = err instanceof Error
          ? err.message
          : 'Failed to fetch servers';
        setError(errorMessage);
      }
      // Only log to console, don't throw
      if (err instanceof Error) {
        console.error('Error fetching servers:', err.message);
      }
    } finally {
      setLoading(false);
    }
  };

  const handleEdit = (server: Server) => {
    setEditingId(server.id);
    if (server.oauth2_access_token_url || server.oauth2_client_id) {
      setIsOAuthExpanded(true);
    }
    setFormData({
      name: server.name,
      transport_type: server.transport_type || 'sse',
      url: server.url || '',
      api_key: server.api_key || '',
      command: server.command || '',
      args: server.args || '',
      env: server.env || '',
      cwd: server.cwd || '',

      system_instruction: server.system_instruction ?? '',
      oauth2_access_token_url: server.oauth2_access_token_url ?? '',
      oauth2_client_id: server.oauth2_client_id ?? '',
      oauth2_client_secret: '', // Never returned by API; leave blank to keep current
      oauth2_scope: server.oauth2_scope ?? '',
    });
  };

  useEffect(() => {
    if (editingId !== null) setIsAddFormExpanded(true);
  }, [editingId]);

  const handleCancelEdit = () => {
    setEditingId(null);
    setIsAddFormExpanded(false);
    setFormData({
      name: '',
      transport_type: 'sse',
      url: '',
      api_key: '',
      command: '',
      args: '',
      env: '',
      cwd: '',

      system_instruction: '',
      oauth2_access_token_url: '',
      oauth2_client_id: '',
      oauth2_client_secret: '',
      oauth2_scope: '',
    });
  };

  const handleDelete = async (serverId: number) => {
    if (!confirm('Are you sure you want to delete this server?')) {
      return;
    }

    try {
      const controller = new AbortController();
      const timeoutId = setTimeout(() => controller.abort(), 10000);

      const response = await fetch(`${apiBaseUrl}/api/servers/${serverId}`, {
        method: 'DELETE',
        signal: controller.signal,
      });
      clearTimeout(timeoutId);

      if (!response.ok) {
        let errorMessage = 'Failed to delete server';
        try {
          const errorData = await response.json();
          errorMessage = errorData.detail || errorMessage;
        } catch {
          errorMessage = `Server error: ${response.status} ${response.statusText}`;
        }
        throw new Error(errorMessage);
      }

      setSuccess('Server deleted successfully!');
      await fetchServers();
    } catch (err) {
      if (isNetworkError(err) || (err instanceof Error && err.name === 'AbortError')) {
        setError(`Cannot connect to backend at ${apiBaseUrl}. Make sure the backend is running.`);
      } else {
        const errorMessage = err instanceof Error
          ? err.message
          : 'Failed to delete server';
        setError(errorMessage);
      }
      if (err instanceof Error) {
        console.error('Error deleting server:', err.message);
      }
    }
  };

  const validateForm = (): string | null => {
    if (!formData.name.trim()) {
      return 'Name is required';
    }

    if (formData.transport_type === 'sse' || formData.transport_type === 'streamable_http') {
      if (!formData.url.trim()) {
        return formData.transport_type === 'sse'
          ? 'URL is required for SSE transport'
          : 'URL is required for Streamable HTTP transport';
      }
    }

    return null;
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    setSuccess(null);

    // Validate form
    const validationError = validateForm();
    if (validationError) {
      setError(validationError);
      setSubmitting(false);
      return;
    }

    try {
      const controller = new AbortController();
      const timeoutId = setTimeout(() => controller.abort(), 10000); // 10 second timeout

      const url = editingId
        ? `${apiBaseUrl}/api/servers/${editingId}`
        : `${apiBaseUrl}/api/servers`;

      // Prepare request body based on transport type
      const requestBody: any = {
        name: formData.name,
        transport_type: formData.transport_type,

        system_instruction: formData.system_instruction.trim() || null,
      };

      if (formData.transport_type === 'sse' || formData.transport_type === 'streamable_http') {
        requestBody.url = formData.url;
        requestBody.api_key = formData.api_key || null;
        requestBody.oauth2_access_token_url = formData.oauth2_access_token_url?.trim() || null;
        requestBody.oauth2_client_id = formData.oauth2_client_id?.trim() || null;
        requestBody.oauth2_client_secret = formData.oauth2_client_secret?.trim() || null;
        requestBody.oauth2_scope = formData.oauth2_scope?.trim() || null;
      }

      const response = await fetch(url, {
        method: editingId ? 'PUT' : 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify(requestBody),
        signal: controller.signal,
      });
      clearTimeout(timeoutId);

      if (!response.ok) {
        let errorMessage = editingId ? 'Failed to update server' : 'Failed to create server';
        try {
          const errorData = await response.json();
          errorMessage = errorData.detail || errorMessage;
        } catch {
          errorMessage = `Server error: ${response.status} ${response.statusText}`;
        }
        throw new Error(errorMessage);
      }

      const server = await response.json();
      setFormData({
        name: '',
        transport_type: 'sse',
        url: '',
        api_key: '',
        command: '',
        args: '',
        env: '',
        cwd: '',

        system_instruction: '',
        oauth2_access_token_url: '',
        oauth2_client_id: '',
        oauth2_client_secret: '',
        oauth2_scope: '',
      });
      setEditingId(null);
      setIsAddFormExpanded(false);
      setSuccess(editingId ? 'Server updated successfully!' : 'Server added successfully!');
      // Refresh the server list to ensure consistency
      await fetchServers();
    } catch (err) {
      if (isNetworkError(err) || (err instanceof Error && err.name === 'AbortError')) {
        setError(`Cannot connect to backend at ${apiBaseUrl}. Make sure the backend is running.`);
      } else {
        const errorMessage = err instanceof Error
          ? err.message
          : editingId ? 'Failed to update server' : 'Failed to add server';
        setError(errorMessage);
      }
      // Only log to console, don't throw
      if (err instanceof Error) {
        console.error('Error saving server:', err.message);
      }
    } finally {
      setSubmitting(false);
    }
  };

  const handleToolVisibilityToggle = async (fullName: string, enabled: boolean) => {
    if (!fullName) return;
    try {
      const response = await fetch(`${apiBaseUrl}/api/servers/tools/visibility`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ tool_name: fullName, enabled }),
      });
      if (!response.ok) {
        const err = await response.json().catch(() => ({}));
        throw new Error(err.detail || 'Failed to update tool visibility');
      }
      await fetchServerTools();
    } catch (err) {
      console.error('Error updating tool visibility:', err);
      setError(err instanceof Error ? err.message : 'Failed to update tool visibility');
    }
  };

  const [savingDescriptionFor, setSavingDescriptionFor] = useState<string | null>(null);
  const [toolDescriptionDraft, setToolDescriptionDraft] = useState<Record<string, string>>({});

  const handleSaveToolDescription = async (fullName: string, description: string) => {
    if (!fullName) return;
    setSavingDescriptionFor(fullName);
    try {
      const response = await fetch(`${apiBaseUrl}/api/servers/tools/description`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ tool_name: fullName, description }),
      });
      if (!response.ok) {
        const err = await response.json().catch(() => ({}));
        throw new Error(err.detail || 'Failed to update tool description');
      }
      setToolDescriptionDraft((prev) => {
        const next = { ...prev };
        delete next[fullName];
        return next;
      });
      await fetchServerTools();
    } catch (err) {
      console.error('Error saving tool description:', err);
      setError(err instanceof Error ? err.message : 'Failed to save tool description');
    } finally {
      setSavingDescriptionFor(null);
    }
  };

  const handleRestoreToolDescription = async (fullName: string) => {
    if (!fullName) return;
    setSavingDescriptionFor(fullName);
    try {
      const response = await fetch(`${apiBaseUrl}/api/servers/tools/description/${encodeURIComponent(fullName)}`, {
        method: 'DELETE',
      });
      if (!response.ok) {
        const err = await response.json().catch(() => ({}));
        throw new Error(err.detail || 'Failed to restore tool description');
      }
      setToolDescriptionDraft((prev) => {
        const next = { ...prev };
        delete next[fullName];
        return next;
      });
      await fetchServerTools();
    } catch (err) {
      console.error('Error restoring tool description:', err);
      setError(err instanceof Error ? err.message : 'Failed to restore tool description');
    } finally {
      setSavingDescriptionFor(null);
    }
  };



  const handleIncludeInLlmChange = async (server: Server, checked: boolean) => {
    try {
      const requestBody: any = {
        name: server.name,
        transport_type: server.transport_type,

        include_in_llm: checked,
      };
      if (server.transport_type === 'sse' || server.transport_type === 'streamable_http') {
        requestBody.url = server.url || null;
        requestBody.api_key = server.api_key || null;
        requestBody.oauth2_access_token_url = server.oauth2_access_token_url ?? null;
        requestBody.oauth2_client_id = server.oauth2_client_id ?? null;
        requestBody.oauth2_scope = server.oauth2_scope ?? null;
        // Do not send oauth2_client_secret so backend keeps current
      } else {
        requestBody.command = server.command || null;
        requestBody.args = server.args || null;
        requestBody.env = server.env || null;
        requestBody.cwd = server.cwd || null;
      }
      const response = await fetch(`${apiBaseUrl}/api/servers/${server.id}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(requestBody),
      });
      if (!response.ok) {
        const err = await response.json().catch(() => ({}));
        throw new Error(err.detail || 'Failed to update server');
      }
      await fetchServers();
      await fetchServerTools();
    } catch (err) {
      console.error('Error updating include in LLM:', err);
      setError(err instanceof Error ? err.message : 'Failed to update server');
    }
  };

  const handleRefreshTools = async () => {
    setRefreshing(true);
    setError(null);
    setSuccess(null);

    try {
      const controller = new AbortController();
      const timeoutId = setTimeout(() => controller.abort(), 30000); // 30 second timeout for refresh

      const response = await fetch(`${apiBaseUrl}/api/servers/refresh`, {
        method: 'POST',
        signal: controller.signal,
      });
      clearTimeout(timeoutId);

      if (!response.ok) {
        let errorMessage = 'Failed to refresh tools';
        try {
          const errorData = await response.json();
          errorMessage = errorData.detail || errorMessage;
        } catch {
          errorMessage = `Server error: ${response.status} ${response.statusText}`;
        }
        throw new Error(errorMessage);
      }

      const data = await response.json();
      setSuccess(`Tools refreshed successfully! ${data.tools_count || 0} tools available.`);
      // Refresh tools display
      await fetchServerTools();
    } catch (err) {
      if (isNetworkError(err) || (err instanceof Error && err.name === 'AbortError')) {
        setError(`Cannot connect to backend at ${apiBaseUrl}. Make sure the backend is running.`);
      } else {
        const errorMessage = err instanceof Error
          ? err.message
          : 'Failed to refresh tools';
        setError(errorMessage);
      }
      // Only log to console, don't throw
      if (err instanceof Error) {
        console.error('Error refreshing tools:', err.message);
      }
    } finally {
      setRefreshing(false);
    }
  };

  return (
    <div className="min-h-screen bg-app-bg text-app-text p-8">
      <div className="max-w-6xl mx-auto">
        <div className="mb-8">
          <div className="flex items-center justify-between mb-4">
            <div>
              <h1 className="text-3xl font-bold text-app-text mb-2">
                Settings
              </h1>
              <p className="text-app-text-muted">
                {activeTab === 'application'
                  ? 'API base URL, CORS, and debug options'
                  : 'Manage your Model Context Protocol (MCP) server connections'}
              </p>
            </div>
            <Link
              href="/"
              className="px-4 py-2 bg-app-border/50 hover:opacity-90 text-app-text rounded-lg transition-colors"
            >
              ← Back to Chat
            </Link>
          </div>
        </div>

        {/* Tabs */}
        <div className="flex gap-2 border-b border-app-border mb-6">
          <button
            type="button"
            onClick={() => setActiveTab('application')}
            className={`px-4 py-2 rounded-t-lg font-medium transition-colors ${activeTab === 'application' ? 'bg-app-border/50 text-app-text' : 'text-app-text-muted hover:text-app-text'}`}
          >
            Application
          </button>
          <button
            type="button"
            onClick={() => setActiveTab('mcp-servers')}
            className={`px-4 py-2 rounded-t-lg font-medium transition-colors ${activeTab === 'mcp-servers' ? 'bg-app-border/50 text-app-text' : 'text-app-text-muted hover:text-app-text'}`}
          >
            MCP Servers
          </button>
        </div>

        {/* Application tab */}
        {activeTab === 'application' && (
          <div className="space-y-6">
            {appSettingsError && (
              <div className="p-4 bg-red-100 dark:bg-red-900 border border-red-400 dark:border-red-700 text-red-700 dark:text-red-300 rounded-lg">
                {appSettingsError}
              </div>
            )}
            {appSettingsSuccess && (
              <div className="p-4 bg-green-100 dark:bg-green-900 border border-green-400 dark:border-green-700 text-green-700 dark:text-green-300 rounded-lg">
                {appSettingsSuccess}
              </div>
            )}
            <div className="bg-white dark:bg-gray-800 rounded-lg shadow p-6">
              <h2 className="text-xl font-semibold mb-4 text-gray-900 dark:text-gray-100">
                Application settings
              </h2>
              {appSettingsLoading ? (
                <p className="text-app-text-muted">Loading settings…</p>
              ) : (
                <form onSubmit={saveAppSettings} className="space-y-4">
                  <div>
                    <label htmlFor="database_url" className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                      Database
                    </label>
                    <input
                      type="text"
                      id="database_url"
                      readOnly
                      value={appSettingsForm.database_url}
                      className="w-full px-3 py-2 border border-gray-300 dark:border-gray-700 rounded-lg bg-gray-100 dark:bg-gray-900 text-gray-600 dark:text-gray-400 cursor-not-allowed"
                    />
                    <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
                      Set via DATABASE_URL in .env; restart required to change.
                    </p>
                  </div>

                  <div className="pt-4 border-t border-gray-200">
                    <h3 className="text-md font-medium text-gray-900 mb-4">Oracle Autonomous Database (Application Persistence)</h3>
                    <p className="text-sm text-gray-500 mb-4">
                      Configure Oracle ADB to replace SQLite. Requires restart to take effect.
                    </p>

                    <div className="mb-4">
                      <label className="block text-sm font-medium text-gray-700 mb-1">
                        Oracle DSN (App DB)
                      </label>
                      <input
                        type="text"
                        value={appSettingsForm.oracle_db_dsn}
                        onChange={(e) => setAppSettingsForm({ ...appSettingsForm, oracle_db_dsn: e.target.value })}
                        className="w-full px-3 py-2 border border-gray-300 rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500"
                        placeholder="e.g. tecpdatp01_high"
                      />
                    </div>

                    <div className="mb-4">
                      <label className="block text-sm font-medium text-gray-700 mb-1">
                        Oracle User (App DB)
                      </label>
                      <input
                        type="text"
                        value={appSettingsForm.oracle_db_user}
                        onChange={(e) => setAppSettingsForm({ ...appSettingsForm, oracle_db_user: e.target.value })}
                        className="w-full px-3 py-2 border border-gray-300 rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500"
                        placeholder="e.g. T2E"
                      />
                    </div>

                    <div className="mb-4">
                      <label className="block text-sm font-medium text-gray-700 mb-1">
                        Oracle Wallet Path (App DB & NL2SQL Shared)
                      </label>
                      <input
                        type="text"
                        value={appSettingsForm.oracle_wallet_path}
                        onChange={(e) => setAppSettingsForm({ ...appSettingsForm, oracle_wallet_path: e.target.value })}
                        className="w-full px-3 py-2 border border-gray-300 rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500"
                        placeholder="/path/to/wallet_dir"
                      />
                      <p className="text-xs text-gray-500 mt-1">
                        Directory containing cwallet.sso. Used if database connection requires a wallet (mTLS).
                      </p>
                    </div>
                  </div>
                  <div>
                    <label htmlFor="oci_config_file" className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                      OCI config file
                    </label>
                    <input
                      type="text"
                      id="oci_config_file"
                      readOnly
                      value={appSettingsForm.oci_config_file}
                      className="w-full px-3 py-2 border border-gray-300 dark:border-gray-700 rounded-lg bg-gray-100 dark:bg-gray-900 text-gray-600 dark:text-gray-400 cursor-not-allowed"
                    />
                    <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
                      Set via OCI_CONFIG_FILE in .env; restart required to change.
                    </p>
                  </div>
                  <div>
                    <label htmlFor="oci_profile" className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                      OCI profile
                    </label>
                    <input
                      type="text"
                      id="oci_profile"
                      readOnly
                      value={appSettingsForm.oci_profile}
                      className="w-full px-3 py-2 border border-gray-300 dark:border-gray-700 rounded-lg bg-gray-100 dark:bg-gray-900 text-gray-600 dark:text-gray-400 cursor-not-allowed"
                    />
                    <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
                      Set via OCI_PROFILE in .env; restart required to change.
                    </p>
                  </div>
                  <div>
                    <label htmlFor="api_base_url" className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                      API base URL <span className="text-red-500">*</span>
                    </label>
                    <input
                      type="url"
                      id="api_base_url"
                      required
                      value={appSettingsForm.api_base_url}
                      onChange={(e) => setAppSettingsForm((f) => ({ ...f, api_base_url: e.target.value }))}
                      className="w-full px-3 py-2 border border-gray-300 dark:border-gray-700 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500 dark:bg-gray-900 dark:text-gray-100"
                      placeholder="http://localhost:8001"
                    />
                  </div>
                  <div>
                    <label htmlFor="cors_origins" className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                      CORS allowed origins (backend)
                    </label>
                    <input
                      type="text"
                      id="cors_origins"
                      value={appSettingsForm.cors_origins}
                      onChange={(e) => setAppSettingsForm((f) => ({ ...f, cors_origins: e.target.value }))}
                      className="w-full px-3 py-2 border border-gray-300 dark:border-gray-700 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500 dark:bg-gray-900 dark:text-gray-100"
                      placeholder="http://localhost:3000"
                    />
                    <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
                      Comma-separated origins. Backend-only; restart may be required for changes.
                    </p>
                  </div>
                  <div>
                    <label htmlFor="debug_log_path" className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                      Debug log path (backend)
                    </label>
                    <input
                      type="text"
                      id="debug_log_path"
                      value={appSettingsForm.debug_log_path}
                      onChange={(e) => setAppSettingsForm((f) => ({ ...f, debug_log_path: e.target.value }))}
                      className="w-full px-3 py-2 border border-gray-300 dark:border-gray-700 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500 dark:bg-gray-900 dark:text-gray-100"
                      placeholder="/path/to/debug.log"
                    />
                    <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
                      Optional. Empty = disabled.
                    </p>
                  </div>
                  <div>
                    <label htmlFor="debug_ingest_url" className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                      Debug ingest URL (web)
                    </label>
                    <input
                      type="url"
                      id="debug_ingest_url"
                      value={appSettingsForm.debug_ingest_url}
                      onChange={(e) => setAppSettingsForm((f) => ({ ...f, debug_ingest_url: e.target.value }))}
                      className="w-full px-3 py-2 border border-gray-300 dark:border-gray-700 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500 dark:bg-gray-900 dark:text-gray-100"
                      placeholder=""
                    />
                    <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
                      Optional. Empty = disabled.
                    </p>
                  </div>
                  <div className="pt-4 mt-6 border-t border-gray-200 dark:border-gray-700">
                    <h3 className="text-lg font-semibold text-gray-900 dark:text-gray-100 mb-3">
                      System Prompt
                    </h3>
                    <p className="text-sm text-gray-500 dark:text-gray-400 mb-3">
                      The full system prompt sent to the LLM. Supports placeholders: <code className="text-xs bg-gray-100 dark:bg-gray-700 px-1 py-0.5 rounded">{'{{TODAY_DATE}}'}</code>, <code className="text-xs bg-gray-100 dark:bg-gray-700 px-1 py-0.5 rounded">{'{{CURRENT_YEAR}}'}</code>, <code className="text-xs bg-gray-100 dark:bg-gray-700 px-1 py-0.5 rounded">{'{{NL2SQL_INSTRUCTIONS}}'}</code>, <code className="text-xs bg-gray-100 dark:bg-gray-700 px-1 py-0.5 rounded">{'{{AGENTIC_TOOLS_INSTRUCTIONS}}'}</code>, <code className="text-xs bg-gray-100 dark:bg-gray-700 px-1 py-0.5 rounded">{'{{DEEPWIKI_INSTRUCTIONS}}'}</code>.
                    </p>
                    <textarea
                      id="system_prompt"
                      rows={12}
                      value={appSettingsForm.system_prompt}
                      onChange={(e) => setAppSettingsForm((f) => ({ ...f, system_prompt: e.target.value }))}
                      className="w-full px-3 py-2 border border-gray-300 dark:border-gray-700 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500 dark:bg-gray-900 dark:text-gray-100 font-mono text-sm"
                      placeholder="Enter system prompt..."
                    />
                  </div>
                  <button
                    type="submit"
                    disabled={appSettingsSaving}
                    className="px-4 py-2 bg-blue-600 hover:bg-blue-700 disabled:bg-gray-400 disabled:cursor-not-allowed text-white rounded-lg transition-colors"
                  >
                    {appSettingsSaving ? 'Saving…' : 'Save'}
                  </button>
                </form>
              )}
            </div>
          </div>
        )}



        {/* MCP Servers tab */}
        {activeTab === 'mcp-servers' && (
          <div className="space-y-6">
            {/* Top bar: Refresh Tools + success/error */}
            <div className="flex flex-wrap items-center gap-4">
              <button
                onClick={handleRefreshTools}
                disabled={refreshing}
                className="px-4 py-2 bg-app-accent hover:opacity-90 disabled:opacity-50 disabled:cursor-not-allowed text-white rounded-lg transition-colors"
              >
                {refreshing ? 'Refreshing...' : 'Refresh Tools'}
              </button>
              {success && (
                <div className="px-4 py-2 bg-green-100 dark:bg-green-900/50 border border-green-400 dark:border-green-700 text-green-700 dark:text-green-300 rounded-lg text-sm">
                  {success}
                </div>
              )}
              {error && (
                <div className="px-4 py-2 bg-red-100 dark:bg-red-900/50 border border-red-400 dark:border-red-700 text-red-700 dark:text-red-300 rounded-lg text-sm">
                  {error}
                </div>
              )}
            </div>

            {/* Add Server button (when collapsed) or Add/Edit Server Form (when expanded) */}
            {!isAddFormExpanded ? (
              <div className="rounded-lg border border-app-border bg-app-surface p-4">
                <button
                  type="button"
                  onClick={() => setIsAddFormExpanded(true)}
                  className="px-4 py-2 bg-app-accent hover:opacity-90 text-white rounded-lg transition-colors"
                >
                  Add Server
                </button>
              </div>
            ) : (
              <div className="rounded-lg border border-app-border bg-app-surface p-6 shadow-sm">
                <h2 className="text-xl font-semibold mb-4 text-app-text">
                  {editingId ? 'Edit Server' : 'Add New Server'}
                </h2>
                <form onSubmit={handleSubmit} className="space-y-4">
                  <div>
                    <label
                      htmlFor="name"
                      className="block text-sm font-medium text-app-text mb-1"
                    >
                      Name
                    </label>
                    <input
                      type="text"
                      id="name"
                      required
                      value={formData.name}
                      onChange={(e) => setFormData({ ...formData, name: e.target.value })}
                      className="w-full px-3 py-2 border border-app-border rounded-lg focus:outline-none focus:ring-2 focus:ring-app-accent bg-app-bg text-app-text"
                      placeholder="Server name"
                    />
                  </div>

                  {/* Transport Type Selector - compact segmented control */}
                  <div>
                    <label className="block text-sm font-medium text-app-text mb-2">
                      Transport Type
                    </label>
                    <div
                      role="tablist"
                      className="inline-flex rounded-lg border border-app-border bg-app-bg p-0.5"
                      aria-label="Transport type"
                    >
                      {(['sse', 'streamable_http'] as const).map((t) => (
                        <button
                          key={t}
                          type="button"
                          role="tab"
                          aria-selected={formData.transport_type === t}
                          onClick={() => setFormData({ ...formData, transport_type: t })}
                          className={`px-4 py-2 text-sm font-medium rounded-md transition-colors focus:outline-none focus:ring-2 focus:ring-app-accent focus:ring-offset-2 focus:ring-offset-app-bg ${formData.transport_type === t
                            ? 'bg-app-accent text-white'
                            : 'text-app-text-muted hover:text-app-text'
                            }`}
                        >
                          {t === 'sse' ? 'SSE' : 'Streamable HTTP'}
                        </button>
                      ))}
                    </div>
                    <p className="mt-1 text-xs text-app-text-muted">
                      Use Streamable HTTP for endpoints like <code className="bg-app-border px-1 rounded">https://mcp.deepwiki.com/mcp</code>.
                    </p>
                  </div>



                  <div>
                    <label
                      htmlFor="system_instruction"
                      className="block text-sm font-medium text-app-text mb-1"
                    >
                      System instruction
                    </label>
                    <textarea
                      id="system_instruction"
                      value={formData.system_instruction}
                      onChange={(e) => setFormData({ ...formData, system_instruction: e.target.value })}
                      className="w-full px-3 py-2 border border-app-border rounded-lg focus:outline-none focus:ring-2 focus:ring-app-accent bg-app-bg text-app-text"
                      placeholder="Optional instructions to include when building the agent system prompt."
                      rows={4}
                    />
                    <p className="mt-1 text-xs text-app-text-muted">
                      Optional. Used later to build the overall system prompt for the agent.
                    </p>
                  </div>

                  {/* SSE / Streamable HTTP Fields (both use URL + optional API key) */}
                  {(formData.transport_type === 'sse' || formData.transport_type === 'streamable_http') && (
                    <>
                      <div>
                        <label
                          htmlFor="url"
                          className="block text-sm font-medium text-app-text mb-1"
                        >
                          URL <span className="text-red-500">*</span>
                        </label>
                        <input
                          type="url"
                          id="url"
                          required
                          value={formData.url}
                          onChange={(e) => setFormData({ ...formData, url: e.target.value })}
                          className="w-full px-3 py-2 border border-app-border rounded-lg focus:outline-none focus:ring-2 focus:ring-app-accent bg-app-bg text-app-text"
                          placeholder="https://example.com/mcp"
                        />
                      </div>
                      <div>
                        <label
                          htmlFor="api_key"
                          className="block text-sm font-medium text-app-text mb-1"
                        >
                          API Key (Optional)
                        </label>
                        <input
                          type="password"
                          id="api_key"
                          value={formData.api_key}
                          onChange={(e) => setFormData({ ...formData, api_key: e.target.value })}
                          className="w-full px-3 py-2 border border-app-border rounded-lg focus:outline-none focus:ring-2 focus:ring-app-accent bg-app-bg text-app-text"
                          placeholder="API key (optional)"
                        />
                      </div>
                      <div className="border-t border-app-border pt-4 mt-2">
                        <button
                          type="button"
                          onClick={() => setIsOAuthExpanded(!isOAuthExpanded)}
                          className="flex items-center gap-2 w-full text-left text-sm font-medium text-app-text hover:text-app-accent transition-colors"
                          aria-expanded={isOAuthExpanded}
                        >
                          <svg
                            className={`w-4 h-4 transition-transform ${isOAuthExpanded ? 'rotate-90' : ''}`}
                            fill="none"
                            stroke="currentColor"
                            viewBox="0 0 24 24"
                          >
                            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" />
                          </svg>
                          OAuth 2.0 (Client Credentials) — Optional
                        </button>
                        {isOAuthExpanded && (
                          <div className="mt-2 space-y-3">
                            <p className="text-xs text-app-text-muted">
                              If OAuth fields are set, they are used instead of API Key for Bearer token (e.g. Oracle Integration Cloud).
                            </p>
                            <div>
                              <label htmlFor="oauth2_access_token_url" className="block text-xs font-medium text-app-text-muted mb-1">
                                Access Token URL
                              </label>
                              <input
                                type="url"
                                id="oauth2_access_token_url"
                                value={formData.oauth2_access_token_url}
                                onChange={(e) => setFormData({ ...formData, oauth2_access_token_url: e.target.value })}
                                className="w-full px-3 py-2 border border-app-border rounded-lg focus:outline-none focus:ring-2 focus:ring-app-accent bg-app-bg text-app-text text-sm"
                                placeholder="https://idcs.example.com/oauth2/v1/token"
                              />
                            </div>
                            <div>
                              <label htmlFor="oauth2_client_id" className="block text-xs font-medium text-app-text-muted mb-1">
                                Client ID
                              </label>
                              <input
                                type="text"
                                id="oauth2_client_id"
                                value={formData.oauth2_client_id}
                                onChange={(e) => setFormData({ ...formData, oauth2_client_id: e.target.value })}
                                className="w-full px-3 py-2 border border-app-border rounded-lg focus:outline-none focus:ring-2 focus:ring-app-accent bg-app-bg text-app-text text-sm"
                                placeholder="OAuth client ID"
                              />
                            </div>
                            <div>
                              <label htmlFor="oauth2_client_secret" className="block text-xs font-medium text-app-text-muted mb-1">
                                Client Secret
                              </label>
                              <input
                                type="password"
                                id="oauth2_client_secret"
                                value={formData.oauth2_client_secret}
                                onChange={(e) => setFormData({ ...formData, oauth2_client_secret: e.target.value })}
                                className="w-full px-3 py-2 border border-app-border rounded-lg focus:outline-none focus:ring-2 focus:ring-app-accent bg-app-bg text-app-text text-sm"
                                placeholder={editingId ? 'Leave blank to keep current' : 'OAuth client secret'}
                              />
                            </div>
                            <div>
                              <label htmlFor="oauth2_scope" className="block text-xs font-medium text-app-text-muted mb-1">
                                Scope (Optional)
                              </label>
                              <input
                                type="text"
                                id="oauth2_scope"
                                value={formData.oauth2_scope}
                                onChange={(e) => setFormData({ ...formData, oauth2_scope: e.target.value })}
                                className="w-full px-3 py-2 border border-app-border rounded-lg focus:outline-none focus:ring-2 focus:ring-app-accent bg-app-bg text-app-text text-sm"
                                placeholder="Optional scope"
                              />
                            </div>
                          </div>
                        )}
                      </div>
                    </>
                  )}


                  <div className="flex gap-2">
                    <button
                      type="submit"
                      disabled={submitting}
                      className="flex-1 px-4 py-2 bg-app-accent hover:opacity-90 disabled:opacity-50 disabled:cursor-not-allowed text-white rounded-lg transition-colors"
                    >
                      {submitting
                        ? (editingId ? 'Updating...' : 'Adding...')
                        : (editingId ? 'Update Server' : 'Add Server')}
                    </button>
                    {(editingId || isAddFormExpanded) && (
                      <button
                        type="button"
                        onClick={handleCancelEdit}
                        disabled={submitting}
                        className="px-4 py-2 bg-app-border hover:opacity-90 disabled:opacity-50 disabled:cursor-not-allowed text-app-text rounded-lg transition-colors"
                      >
                        Cancel
                      </button>
                    )}
                  </div>
                </form>
              </div>
            )}

            {/* Servers Table */}
            <div className="rounded-lg border border-app-border bg-app-surface p-6 shadow-sm">
              <h2 className="text-xl font-semibold mb-4 text-app-text">
                Configured Servers
              </h2>
              {loading ? (
                <div className="text-center py-8 text-app-text-muted">
                  Loading servers...
                </div>
              ) : servers.length === 0 ? (
                <div className="text-center py-8 text-app-text-muted">
                  No servers configured yet
                </div>
              ) : (
                <div className="space-y-4">
                  {servers.map((server) => {
                    const tools = serverTools[server.name] || [];
                    const isExpanded = expandedServers.has(server.id);
                    const serverInclude = server.include_in_llm ?? true;

                    return (
                      <div
                        key={server.id}
                        className="rounded-lg border border-app-border overflow-hidden bg-app-bg"
                      >
                        {/* Server Header - clearer hierarchy, responsive layout */}
                        <div className="p-4 flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3">
                          <div className="flex-1 min-w-0">
                            <div className="flex items-center gap-2 flex-wrap">
                              <span className="text-sm font-medium text-app-text">
                                {server.name}
                              </span>
                              <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${server.transport_type === 'sse'
                                ? 'bg-blue-100 text-blue-800 dark:bg-blue-900/50 dark:text-blue-300'
                                : server.transport_type === 'streamable_http'
                                  ? 'bg-cyan-100 text-cyan-800 dark:bg-cyan-900/50 dark:text-cyan-300'
                                  : 'bg-purple-100 text-purple-800 dark:bg-purple-900/50 dark:text-purple-300'
                                }`}>
                                {server.transport_type === 'sse' ? 'SSE' : server.transport_type === 'streamable_http' ? 'Streamable HTTP' : 'Stdio'}
                              </span>
                              <span
                                className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium ${server.is_active
                                  ? 'bg-green-100 text-green-800 dark:bg-green-900/50 dark:text-green-300'
                                  : 'bg-app-border text-app-text-muted'
                                  }`}
                              >
                                {server.is_active ? 'Active' : 'Inactive'}
                              </span>
                            </div>
                            <p className="mt-1 text-xs text-app-text-muted truncate">
                              {(server.transport_type === 'sse' || server.transport_type === 'streamable_http')
                                ? server.url || 'N/A'
                                : server.command || 'N/A'}
                            </p>
                          </div>
                          <div className="flex items-center gap-3 flex-wrap">
                            <div className="flex items-center gap-3" title="Server-level controls">
                              <label className="flex items-center gap-1.5 cursor-pointer" title="When disabled, no tools from this server are sent to the LLM">
                                <input
                                  type="checkbox"
                                  checked={serverInclude}
                                  onChange={(e) => handleIncludeInLlmChange(server, e.target.checked)}
                                  className="rounded border-app-border text-app-accent focus:ring-app-accent"
                                />
                                <span className="text-xs text-app-text-muted">Include</span>
                              </label>

                            </div>
                            <div className="flex gap-2 border-l border-app-border pl-3">
                              <button
                                onClick={() => handleEdit(server)}
                                className="px-3 py-1.5 text-xs bg-app-accent hover:opacity-90 text-white rounded transition-colors"
                              >
                                Edit
                              </button>
                              <button
                                onClick={() => handleDelete(server.id)}
                                className="px-3 py-1.5 text-xs bg-red-600 hover:bg-red-700 text-white rounded transition-colors"
                              >
                                Delete
                              </button>
                            </div>
                          </div>
                        </div>

                        {/* Tools Section */}
                        {tools.length > 0 && (
                          <div className="border-t border-app-border">
                            <button
                              onClick={() => toggleServerExpanded(server.id)}
                              className="w-full p-3 flex items-center justify-between hover:bg-app-surface transition-colors"
                            >
                              <span className="text-sm font-medium text-app-text">
                                Tools ({tools.length})
                              </span>
                              <svg
                                className={`w-5 h-5 text-app-text-muted transition-transform ${isExpanded ? 'transform rotate-180' : ''
                                  }`}
                                fill="none"
                                stroke="currentColor"
                                viewBox="0 0 24 24"
                              >
                                <path
                                  strokeLinecap="round"
                                  strokeLinejoin="round"
                                  strokeWidth={2}
                                  d="M19 9l-7 7-7-7"
                                />
                              </svg>
                            </button>

                            {isExpanded && (
                              <div className={`p-4 bg-app-surface space-y-3 ${!serverInclude ? 'opacity-60' : ''}`}>
                                {tools.map((tool, idx) => {
                                  const fullName = tool.full_name ?? '';
                                  const detailsExpanded = expandedToolDetails.has(fullName);
                                  const desc = (toolDescriptionDraft[fullName] ?? tool.description ?? '').trim();
                                  const preview = desc.length > 80 ? desc.slice(0, 80) + '…' : desc;
                                  return (
                                    <div
                                      key={idx}
                                      className="rounded-lg p-4 border border-app-border bg-app-bg"
                                    >
                                      <div className="flex items-center justify-between gap-3">
                                        <h4 className="text-sm font-semibold text-app-text">
                                          {tool.name}
                                          {(tool.original_description !== undefined && tool.description !== tool.original_description) && (
                                            <span className="ml-2 inline-flex items-center px-1.5 py-0.5 rounded text-xs font-medium bg-amber-100 text-amber-800 dark:bg-amber-900/50 dark:text-amber-300">
                                              Edited
                                            </span>
                                          )}
                                        </h4>
                                        <label
                                          className={`flex items-center gap-2 shrink-0 ${serverInclude ? 'cursor-pointer' : 'cursor-not-allowed opacity-60'}`}
                                          title={
                                            !serverInclude
                                              ? 'Disabled because server is not included'
                                              : tool.enabled !== false
                                                ? 'Visible to LLM'
                                                : 'Hidden from LLM'
                                          }
                                        >
                                          <input
                                            type="checkbox"
                                            checked={tool.enabled !== false}
                                            onChange={(e) => handleToolVisibilityToggle(fullName, e.target.checked)}
                                            disabled={!serverInclude}
                                            className="rounded border-app-border text-app-accent focus:ring-app-accent"
                                          />
                                          <span className="text-xs text-app-text-muted">Send to LLM</span>
                                        </label>
                                      </div>
                                      {!serverInclude && (
                                        <p className="text-xs text-app-text-muted mt-2 mb-2">
                                          Tools from this server are currently not included in the LLM payload.
                                        </p>
                                      )}
                                      {!detailsExpanded ? (
                                        <div className="mt-2 flex items-center justify-between gap-2">
                                          <p className="text-xs text-app-text-muted truncate flex-1 min-w-0">
                                            {preview || 'No description'}
                                          </p>
                                          <button
                                            type="button"
                                            onClick={() => toggleToolDetailsExpanded(fullName)}
                                            className="text-xs text-app-accent hover:underline shrink-0"
                                          >
                                            Edit
                                          </button>
                                        </div>
                                      ) : (
                                        <div className="mt-3 space-y-3">
                                          <div>
                                            <label className="block text-xs font-medium text-app-text mb-1">
                                              Description (used in LLM payload)
                                            </label>
                                            <textarea
                                              value={toolDescriptionDraft[fullName] ?? tool.description ?? ''}
                                              onChange={(e) => setToolDescriptionDraft((prev) => ({ ...prev, [fullName]: e.target.value }))}
                                              onBlur={(e) => {
                                                const value = (toolDescriptionDraft[fullName] ?? tool.description ?? '').trim();
                                                if (value !== (tool.description ?? '')) {
                                                  handleSaveToolDescription(fullName, value);
                                                }
                                              }}
                                              disabled={savingDescriptionFor === fullName}
                                              className="w-full px-3 py-2 text-sm border border-app-border rounded-lg focus:outline-none focus:ring-2 focus:ring-app-accent bg-app-bg text-app-text resize-y min-h-[60px]"
                                              placeholder="Tool description for the LLM"
                                              rows={2}
                                            />
                                            <div className="flex items-center gap-2 mt-1">
                                              {(tool.original_description !== undefined && (tool.description !== tool.original_description || (toolDescriptionDraft[fullName] ?? tool.description) !== tool.original_description)) && (
                                                <button
                                                  type="button"
                                                  onClick={() => handleRestoreToolDescription(fullName)}
                                                  disabled={savingDescriptionFor === fullName}
                                                  className="text-xs px-2 py-1 bg-app-border hover:opacity-90 text-app-text rounded transition-colors disabled:opacity-50"
                                                >
                                                  Restore original
                                                </button>
                                              )}
                                              {savingDescriptionFor === fullName && (
                                                <span className="text-xs text-app-text-muted">Saving…</span>
                                              )}
                                              <button
                                                type="button"
                                                onClick={() => toggleToolDetailsExpanded(fullName)}
                                                className="text-xs text-app-text-muted hover:text-app-text"
                                              >
                                                Collapse
                                              </button>
                                            </div>
                                          </div>
                                          {tool.inputSchema && tool.inputSchema.properties && (
                                            <div className="mt-3">
                                              <p className="text-xs font-medium text-app-text mb-2">
                                                Parameters:
                                              </p>
                                              <div className="space-y-1">
                                                {Object.entries(tool.inputSchema.properties).map(([paramName, paramSchema]: [string, any]) => (
                                                  <div
                                                    key={paramName}
                                                    className="text-xs text-app-text-muted flex items-center gap-2"
                                                  >
                                                    <span className="font-mono font-medium">{paramName}</span>
                                                    <span>
                                                      ({paramSchema.type || 'string'})
                                                    </span>
                                                    {tool.inputSchema.required?.includes(paramName) && (
                                                      <span className="text-red-600 dark:text-red-400 text-xs">
                                                        required
                                                      </span>
                                                    )}
                                                  </div>
                                                ))}
                                              </div>
                                            </div>
                                          )}
                                        </div>
                                      )
                                      }
                                    </div>
                                  );
                                })}
                              </div>
                            )}
                          </div>
                        )
                        }

                        {
                          tools.length === 0 && (
                            <div className="p-3 text-sm text-app-text-muted text-center border-t border-app-border">
                              No tools available. Click "Refresh Tools" to load tools from this server.
                            </div>
                          )
                        }
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          </div>
        )}
      </div>
    </div >
  );
}

