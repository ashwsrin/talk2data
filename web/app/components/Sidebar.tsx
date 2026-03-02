'use client';

import { useState, useEffect, useRef } from 'react';
import { useApiConfig } from '../contexts/ApiConfigContext';
import { useNavigation } from '../contexts/NavigationContext';

interface Conversation {
  id: number;
  title: string;
  user_name: string;
  created_at: string;
  updated_at: string;
}

interface SidebarProps {
  onToggle: () => void;
  currentConversationId: number | null;
  onConversationSelect: (conversationId: number | null) => void;
  onNewConversation: () => void;
  onRegisterRefresh?: (refresh: () => void) => void;
}

export default function Sidebar({
  onToggle,
  currentConversationId,
  onConversationSelect,
  onNewConversation,
  onRegisterRefresh
}: SidebarProps) {
  const { apiBaseUrl } = useApiConfig();
  const { navigateToSettings } = useNavigation();
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [loading, setLoading] = useState(true);
  const [searchQuery, setSearchQuery] = useState('');
  const [searchExpanded, setSearchExpanded] = useState(false);
  const searchInputRef = useRef<HTMLInputElement>(null);
  const [deleteConfirmId, setDeleteConfirmId] = useState<number | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [editingTitle, setEditingTitle] = useState('');
  const [savingTitle, setSavingTitle] = useState(false);
  const editingIdRef = useRef<number | null>(null);

  useEffect(() => {
    editingIdRef.current = editingId;
  }, [editingId]);

  useEffect(() => {
    if (searchExpanded) searchInputRef.current?.focus();
  }, [searchExpanded]);

  const fetchConversations = async (silent?: boolean) => {
    try {
      if (!silent) setLoading(true);
      const response = await fetch(`${apiBaseUrl}/api/conversations`);
      if (response.ok) {
        const data = await response.json();
        if (silent && editingIdRef.current !== null) return;
        setConversations(data);
      }
    } catch (error) {
      console.error('Error fetching conversations:', error);
    } finally {
      if (!silent) setLoading(false);
    }
  };

  useEffect(() => {
    fetchConversations();
    // Refresh conversations every 5 seconds to catch updates (silent: no loading, no apply while editing)
    const interval = setInterval(() => fetchConversations(true), 5000);
    return () => clearInterval(interval);
  }, [apiBaseUrl]);

  useEffect(() => {
    if (!onRegisterRefresh) return;
    onRegisterRefresh(() => {
      fetchConversations(true);
    });
  }, [onRegisterRefresh, apiBaseUrl]);

  const filteredConversations = searchQuery.trim()
    ? conversations.filter((c) =>
      c.title.toLowerCase().includes(searchQuery.trim().toLowerCase())
    )
    : conversations;

  type DateBucket = 'today' | 'last7' | 'last30' | 'older';
  const getDateBucket = (updatedAt: string): DateBucket => {
    const updated = new Date(updatedAt.includes('Z') ? updatedAt : updatedAt + 'Z');
    const now = new Date();
    const updatedDayStart = new Date(updated.getFullYear(), updated.getMonth(), updated.getDate()).getTime();
    const todayDayStart = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
    const diffDays = Math.floor((todayDayStart - updatedDayStart) / 86400000);
    if (diffDays === 0) return 'today';
    if (diffDays >= 1 && diffDays <= 6) return 'last7';
    if (diffDays >= 7 && diffDays <= 29) return 'last30';
    return 'older';
  };

  const bucketOrder: DateBucket[] = ['today', 'last7', 'last30', 'older'];
  const bucketLabels: Record<DateBucket, string> = {
    today: 'Today',
    last7: 'Last 7 days',
    last30: 'Last 30 days',
    older: 'Older',
  };
  const groupedByBucket = filteredConversations.reduce<Record<DateBucket, Conversation[]>>(
    (acc, conv) => {
      const b = getDateBucket(conv.updated_at);
      if (!acc[b]) acc[b] = [];
      acc[b].push(conv);
      return acc;
    },
    { today: [], last7: [], last30: [], older: [] }
  );

  const formatDateTime = (dateString: string) => {
    // Ensure dateString is treated as UTC (append 'Z' if not present)
    const utcDateString = dateString.includes('Z') ? dateString : dateString + 'Z';
    const date = new Date(utcDateString);

    // Get current time in UTC
    const now = new Date();
    const nowUTC = Date.UTC(
      now.getUTCFullYear(),
      now.getUTCMonth(),
      now.getUTCDate(),
      now.getUTCHours(),
      now.getUTCMinutes(),
      now.getUTCSeconds()
    );

    // Get date time in UTC (timestamp)
    const dateUTC = date.getTime();

    // Calculate difference in UTC
    const diffMs = nowUTC - dateUTC;
    const diffMins = Math.floor(diffMs / 60000);
    const diffHours = Math.floor(diffMs / 3600000);
    const diffDays = Math.floor(diffMs / 86400000);

    if (diffMins < 1) return 'Just now';
    if (diffMins < 60) return `${diffMins}m ago`;
    if (diffHours < 24) return `${diffHours}h ago`;
    if (diffDays < 7) return `${diffDays}d ago`;

    // Format absolute date in user's local timezone
    const localYear = date.getFullYear();
    const localMonth = date.getMonth();
    const localDay = date.getDate();
    const monthNames = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
    const nowYear = now.getFullYear();

    return `${monthNames[localMonth]} ${localDay}${localYear !== nowYear ? `, ${localYear}` : ''}`;
  };

  const handleNewChat = () => {
    onNewConversation();
    // Refresh conversations after a short delay to show the new one
    setTimeout(fetchConversations, 500);
  };

  const handleDeleteConversation = async (conversationId: number) => {
    setDeleting(true);
    try {
      const response = await fetch(`${apiBaseUrl}/api/conversations/${conversationId}`, {
        method: 'DELETE',
      });
      if (response.ok) {
        // If we deleted the current conversation, clear selection
        if (currentConversationId === conversationId) {
          onConversationSelect(null);
        }
        fetchConversations();
      }
    } catch (error) {
      console.error('Error deleting conversation:', error);
    } finally {
      setDeleting(false);
      setDeleteConfirmId(null);
    }
  };

  const startEditingTitle = (conv: Conversation) => {
    setEditingId(conv.id);
    setEditingTitle(conv.title);
  };

  const cancelEditingTitle = () => {
    setEditingId(null);
    setEditingTitle('');
  };

  const saveEditingTitle = async () => {
    if (editingId === null) return;
    const newTitle = editingTitle.trim() || 'New Conversation';
    setSavingTitle(true);
    try {
      const response = await fetch(`${apiBaseUrl}/api/conversations/${editingId}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title: newTitle }),
      });
      if (response.ok) {
        setConversations((prev) =>
          prev.map((c) => (c.id === editingId ? { ...c, title: newTitle } : c))
        );
        cancelEditingTitle();
      }
    } catch (error) {
      console.error('Error updating conversation title:', error);
    } finally {
      setSavingTitle(false);
    }
  };

  return (
    <div className="h-full w-full bg-app-sidebar-bg border-r border-app-border p-4">
      <div className="flex flex-col h-full">
        {/* Header with New Chat icon and collapse button */}
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-xl font-semibold text-app-text">
            Chat History
          </h2>
          <div className="flex items-center gap-1">
            <button
              onClick={() => setSearchExpanded((prev) => !prev)}
              className={`p-1.5 rounded-md transition-colors text-app-text-muted hover:bg-app-sidebar-item-hover ${searchExpanded ? 'bg-app-sidebar-item-hover' : ''}`}
              title="Search chat history"
              aria-label="Search chat history"
            >
              <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
              </svg>
            </button>
            <button
              onClick={handleNewChat}
              className="p-1.5 rounded-md hover:bg-app-sidebar-item-hover transition-colors text-app-text-muted"
              title="New Chat"
              aria-label="New Chat"
            >
              <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 4v16m8-8H4" />
              </svg>
            </button>
            <button
              onClick={onToggle}
              className="p-1.5 rounded-md hover:bg-app-sidebar-item-hover transition-colors"
              aria-label="Collapse sidebar"
            >
              <svg
                className="w-5 h-5 text-app-text-muted"
                fill="none"
                stroke="currentColor"
                viewBox="0 0 24 24"
              >
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  strokeWidth={2}
                  d="M15 19l-7-7 7-7"
                />
              </svg>
            </button>
          </div>
        </div>

        {searchExpanded && (
          <div className="mb-3">
            <div className="relative">
              <span className="absolute left-2.5 top-1/2 -translate-y-1/2 text-app-text-muted pointer-events-none">
                <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
                </svg>
              </span>
              <input
                ref={searchInputRef}
                type="text"
                placeholder="Search"
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                className="w-full pl-8 pr-3 py-2 text-sm rounded-lg border border-app-border bg-app-bg text-app-text placeholder-app-text-muted focus:outline-none focus:ring-2 focus:ring-app-accent focus:border-transparent"
                aria-label="Search chat history by title"
              />
            </div>
          </div>
        )}

        {/* Conversation list */}
        <div className="flex-1 overflow-y-auto mb-4">
          {loading ? (
            <div className="space-y-2 px-1">
              {[...Array(5)].map((_, i) => (
                <div key={i} className="py-2 px-2.5 rounded-lg bg-app-sidebar-item-bg border-l-4 border-transparent animate-pulse">
                  <div className="h-4 bg-app-border/50 rounded w-3/4 mb-2" />
                  <div className="h-3 bg-app-border/30 rounded w-1/3" />
                </div>
              ))}
            </div>
          ) : conversations.length === 0 ? (
            <div className="text-center py-4 text-app-text-muted text-sm">
              No conversations yet
            </div>
          ) : filteredConversations.length === 0 ? (
            <div className="text-center py-4 text-app-text-muted text-sm">
              No conversations match your search
            </div>
          ) : (
            <div className="space-y-2">
              {bucketOrder.map((bucket) => {
                const list = groupedByBucket[bucket];
                if (!list.length) return null;
                return (
                  <div key={bucket}>
                    <h3 className="text-xs font-semibold text-app-text-muted uppercase tracking-wide mb-1 px-1">
                      {bucketLabels[bucket]}
                    </h3>
                    <div className="space-y-0.5">
                      {list.map((conversation) => (
                        <div
                          key={conversation.id}
                          className={`group flex items-center gap-1 py-2 px-2.5 rounded-lg transition-colors border-l-4 border-app-selected-border ${currentConversationId === conversation.id
                            ? 'bg-app-selected-bg text-app-text'
                            : 'bg-app-sidebar-item-bg hover:bg-app-sidebar-item-hover text-app-sidebar-item-text border-transparent'
                            }`}
                        >
                          {editingId === conversation.id ? (
                            <>
                              <div className="flex-1 flex items-center gap-1 min-w-0">
                                <input
                                  type="text"
                                  value={editingTitle}
                                  onChange={(e) => setEditingTitle(e.target.value)}
                                  onKeyDown={(e) => {
                                    if (e.key === 'Enter') {
                                      e.preventDefault();
                                      saveEditingTitle();
                                    }
                                    if (e.key === 'Escape') {
                                      e.preventDefault();
                                      cancelEditingTitle();
                                    }
                                  }}
                                  onBlur={() => cancelEditingTitle()}
                                  disabled={savingTitle}
                                  className="flex-1 min-w-0 text-sm font-medium bg-app-surface border border-app-border rounded px-2 py-1 text-app-text focus:outline-none focus:ring-2 focus:ring-app-accent"
                                  onClick={(e) => e.stopPropagation()}
                                  aria-label="Edit conversation title"
                                />
                                <button
                                  type="button"
                                  onMouseDown={(e) => e.preventDefault()}
                                  onClick={(e) => { e.stopPropagation(); saveEditingTitle(); }}
                                  disabled={savingTitle}
                                  className="p-1.5 rounded hover:bg-green-100 dark:hover:bg-green-900/30 text-app-text-muted hover:text-green-600 dark:hover:text-green-400 transition-colors shrink-0 disabled:opacity-50"
                                  title="Save title"
                                  aria-label="Save title"
                                >
                                  <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
                                  </svg>
                                </button>
                              </div>
                            </>
                          ) : (
                            <button
                              onClick={() => onConversationSelect(conversation.id)}
                              className="flex-1 text-left min-w-0 leading-tight"
                            >
                              <p className="text-sm font-medium truncate leading-snug">{conversation.title}</p>
                              <p className="text-xs text-app-text-muted mt-0.5 leading-snug">
                                {formatDateTime(conversation.updated_at)}
                              </p>
                            </button>
                          )}
                          {editingId !== conversation.id && (
                            <button
                              onClick={(e) => { e.stopPropagation(); startEditingTitle(conversation); }}
                              className="p-1.5 rounded opacity-0 group-hover:opacity-100 hover:bg-app-sidebar-item-hover text-app-text-muted hover:text-app-text transition-opacity"
                              title="Edit conversation title"
                              aria-label="Edit conversation title"
                            >
                              <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15.232 5.232l3.536 3.536m-2.036-5.036a2.5 2.5 0 113.536 3.536L6.5 21.036H3v-3.572L16.732 3.732z" />
                              </svg>
                            </button>
                          )}
                          <button
                            onClick={(e) => { e.stopPropagation(); setDeleteConfirmId(conversation.id); }}
                            className="p-1.5 rounded opacity-0 group-hover:opacity-100 hover:bg-red-100 dark:hover:bg-red-900/30 text-app-text-muted hover:text-red-600 dark:hover:text-red-400 transition-opacity"
                            title="Delete conversation"
                            aria-label="Delete conversation"
                          >
                            <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
                            </svg>
                          </button>
                        </div>
                      ))}
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>

      </div>

      {/* Delete confirmation modal */}
      {
        deleteConfirmId !== null && (
          <div
            className="fixed inset-0 z-50 flex items-center justify-center bg-black/50"
            onClick={() => setDeleteConfirmId(null)}
          >
            <div
              className="bg-app-surface rounded-lg p-6 max-w-sm w-full mx-4 shadow-xl"
              onClick={(e) => e.stopPropagation()}
            >
              <h3 className="text-lg font-semibold text-app-text mb-2">
                Delete Conversation?
              </h3>
              <p className="text-sm text-app-text-muted mb-4">
                This will permanently delete this conversation and all its messages. This action cannot be undone.
              </p>
              <div className="flex justify-end gap-2">
                <button
                  onClick={() => setDeleteConfirmId(null)}
                  disabled={deleting}
                  className="px-4 py-2 text-sm rounded bg-app-border hover:opacity-90 text-app-text transition-colors disabled:opacity-50"
                >
                  Cancel
                </button>
                <button
                  onClick={() => handleDeleteConversation(deleteConfirmId)}
                  disabled={deleting}
                  className="px-4 py-2 text-sm rounded bg-red-600 hover:bg-red-700 text-white transition-colors disabled:opacity-50"
                >
                  {deleting ? 'Deleting...' : 'Delete'}
                </button>
              </div>
            </div>
          </div>
        )
      }
    </div >
  );
}
