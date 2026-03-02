'use client';

import { useRef, useState, useEffect } from 'react';
import { useSearchParams, useRouter, usePathname } from 'next/navigation';
import Sidebar from './components/Sidebar';
import ChatInterface from './components/ChatInterface';
import Header from './components/Header';

function parseConversationId(param: string | null): number | null {
  if (!param) return null;
  const num = parseInt(param, 10);
  return Number.isNaN(num) ? null : num;
}

export default function HomeContent() {
  const [isSidebarExpanded, setIsSidebarExpanded] = useState(true);
  const [sidebarWidth, setSidebarWidth] = useState(260);
  const [isResizing, setIsResizing] = useState(false);
  const sidebarRef = useRef<HTMLDivElement>(null);

  const searchParams = useSearchParams();
  const router = useRouter();
  const pathname = usePathname();
  const refreshConversationsRef = useRef<null | (() => void)>(null);

  const currentConversationId = parseConversationId(searchParams.get('conversation'));

  const startResizing = (e: React.MouseEvent) => {
    setIsResizing(true);
    e.preventDefault(); // Prevent text selection
  };

  const stopResizing = () => {
    setIsResizing(false);
  };

  const resize = (e: MouseEvent) => {
    if (isResizing) {
      // Limit width between 200px and 480px
      const newWidth = Math.max(200, Math.min(480, e.clientX));
      setSidebarWidth(newWidth);
    }
  };

  // Add global event listeners for drag
  useEffect(() => {
    if (isResizing) {
      window.addEventListener('mousemove', resize);
      window.addEventListener('mouseup', stopResizing);
    } else {
      window.removeEventListener('mousemove', resize);
      window.removeEventListener('mouseup', stopResizing);
    }
    return () => {
      window.removeEventListener('mousemove', resize);
      window.removeEventListener('mouseup', stopResizing);
    };
  }, [isResizing]);

  const handleConversationSelect = (convId: number | null) => {
    const params = new URLSearchParams(searchParams.toString());
    if (convId !== null) {
      params.set('conversation', String(convId));
    } else {
      params.delete('conversation');
    }
    const query = params.toString();
    const target = query ? `${pathname ?? '/'}?${query}` : pathname ?? '/';
    router.replace(target, { scroll: false });
  };

  const handleNewConversation = () => {
    handleConversationSelect(null);
  };

  return (
    <div className="flex flex-col h-screen w-full overflow-hidden bg-app-bg" onMouseUp={stopResizing}>
      <Header />
      <div className="flex flex-1 overflow-hidden relative">
        {isSidebarExpanded && (
          <div
            ref={sidebarRef}
            className={`flex-shrink-0 h-full border-r border-app-border relative flex flex-col ${isResizing ? '' : 'transition-all duration-300'}`}
            style={{ width: sidebarWidth }}
          >
            <Sidebar
              onToggle={() => setIsSidebarExpanded(false)}
              currentConversationId={currentConversationId}
              onConversationSelect={handleConversationSelect}
              onNewConversation={handleNewConversation}
              onRegisterRefresh={(refresh) => {
                refreshConversationsRef.current = refresh;
              }}
            />

            {/* Drag Handle */}
            <div
              className={`absolute top-0 right-[-4px] w-2 h-full cursor-col-resize z-50 hover:bg-app-accent/20 transition-colors ${isResizing ? 'bg-app-accent/20' : ''}`}
              onMouseDown={startResizing}
              title="Drag to resize sidebar"
            />
          </div>
        )}
        {!isSidebarExpanded && (
          <button
            onClick={() => setIsSidebarExpanded(true)}
            className="absolute left-0 top-[calc(4rem+50%)] -translate-y-1/2 z-10 p-2 bg-app-surface hover:bg-app-sidebar-item-hover border-r border-app-border rounded-r-lg transition-colors shadow-md"
            aria-label="Expand sidebar"
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
                d="M9 5l7 7-7 7"
              />
            </svg>
          </button>
        )}
        <div className="flex-1 h-full min-w-0 transition-all duration-300">
          <ChatInterface
            conversationId={currentConversationId}
            onConversationCreated={handleConversationSelect}
            refreshConversations={() => refreshConversationsRef.current?.()}
          />
        </div>
      </div>
    </div>
  );
}
