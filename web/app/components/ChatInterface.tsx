'use client';

import React, { useState, useEffect, useRef, useMemo } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import mermaid from 'mermaid';
import ArtifactRenderer from './ArtifactRenderer';
import VegaLiteEmbed from './VegaLiteEmbed';

import PaginatedTable from './PaginatedTable';
import { useApiConfig } from '../contexts/ApiConfigContext';

interface ChatInterfaceProps {
    conversationId: number | null;
    onConversationCreated: (conversationId: number) => void;
    refreshConversations?: () => void;
}

export default function ChatInterface({ conversationId, onConversationCreated, refreshConversations }: ChatInterfaceProps) {
    const { apiBaseUrl, appSettings } = useApiConfig();
    // Local state for messages
    const [messages, setMessages] = useState<any[]>([]);
    const [isLoading, setIsLoading] = useState(false);
    const [messagesLoading, setMessagesLoading] = useState(false);
    const [error, setError] = useState<Error | null>(null);
    const [inputValue, setInputValue] = useState('');
    const [currentConversationId, setCurrentConversationId] = useState<number | null>(conversationId);

    // Ref to track if we're currently sending a message (to prevent reload overwriting local state)
    const isSendingMessage = useRef(false);
    // Ref to track the conversation we just created (to prevent immediate reload)
    const justCreatedConversationId = useRef<number | null>(null);
    // Ref for latest apiBaseUrl (avoids effect re-runs when apiBaseUrl changes from refreshSettings)
    const apiBaseUrlRef = useRef(apiBaseUrl);
    apiBaseUrlRef.current = apiBaseUrl;
    // Execution details modal: which message to show, fetched data
    const [executionDetailsMessageId, setExecutionDetailsMessageId] = useState<number | null>(null);
    const [executionDetailsData, setExecutionDetailsData] = useState<{
        prompt_messages: Array<{ role: string; content: string; tool_calls?: Array<{ name: string; id: string }> }>;
        tool_calls: Array<{
            sequence: number;
            name: string;
            args: Record<string, unknown>;
            output: string;
            invoked_at?: string;
            duration_ms?: number;
        }>;
        full_prompt?: unknown;
        raw_response?: unknown;
        graph_mermaid?: string;
    } | null>(null);
    const [executionDetailsLoading, setExecutionDetailsLoading] = useState(false);
    const [executionDetailsError, setExecutionDetailsError] = useState<string | null>(null);
    const [copiedSection, setCopiedSection] = useState<string | null>(null);
    const [copiedMessageId, setCopiedMessageId] = useState<string | null>(null);
    // Scroll adjustment refs
    const messagesEndRef = useRef<HTMLDivElement>(null);
    const chatContainerRef = useRef<HTMLDivElement>(null);
    const [copiedCodeBlockKey, setCopiedCodeBlockKey] = useState<string | null>(null);
    const graphMermaidRef = useRef<HTMLDivElement>(null);


    // Defensive: never render image data URLs (base64) inside execution-details prompt view.
    const sanitizeExecutionPromptContent = (raw: unknown): string => {
        const s = typeof raw === 'string' ? raw : raw == null ? '' : String(raw);
        if (!s.includes('data:image/')) return s;
        let out = '';
        let idx = 0;
        let imgN = 0;
        while (true) {
            const start = s.indexOf('data:image/', idx);
            if (start === -1) {
                out += s.slice(idx);
                break;
            }
            out += s.slice(idx, start);
            // Find end at first whitespace or quote (data URL itself won't contain quotes/spaces)
            let end = start;
            while (end < s.length) {
                const ch = s[end];
                if (ch === ' ' || ch === '\n' || ch === '\t' || ch === '\'' || ch === '"') break;
                end++;
            }
            const dataUrl = s.slice(start, end);
            const header = dataUrl.split(',', 1)[0];
            const mime = header.startsWith('data:') ? header.slice(5).split(';', 1)[0] : '';
            const ext =
                mime === 'image/jpeg'
                    ? 'jpg'
                    : mime === 'image/png'
                        ? 'png'
                        : mime === 'image/webp'
                            ? 'webp'
                            : mime === 'image/gif'
                                ? 'gif'
                                : 'bin';
            imgN += 1;
            out += `[Image: image-${imgN}.${ext}]`;
            idx = end;
        }
        return out;
    };

    const copyExecutionSection = (text: string, sectionKey: string) => {
        navigator.clipboard.writeText(text).then(
            () => {
                setCopiedSection(sectionKey);
                setTimeout(() => setCopiedSection(null), 2000);
            },
            () => { }
        );
    };

    const copyMessageContent = (content: string, messageKey: string) => {
        const text = typeof content === 'string' ? content : '';
        navigator.clipboard.writeText(text).then(
            () => {
                setCopiedMessageId(messageKey);
                setTimeout(() => setCopiedMessageId(null), 2000);
            },
            () => { }
        );
    };

    const codeBlockCopyKey = (codeString: string) =>
        `code-${codeString.slice(0, 40)}`;

    const copyCodeBlock = (codeString: string) => {
        navigator.clipboard.writeText(codeString).then(() => {
            setCopiedCodeBlockKey(codeBlockCopyKey(codeString));
            setTimeout(() => setCopiedCodeBlockKey(null), 2000);
        });
    };





    // Input change handler
    const onInputChange = (e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) => {
        setInputValue(e.target.value);
    };

    // Dedicated clear when switching to New Chat - ensures messages reset even if load effect has edge cases.
    useEffect(() => {
        if (conversationId === null) {
            setMessages([]);
            setCurrentConversationId(null);
            setError(null);
            setExecutionDetailsMessageId(null);
        }
    }, [conversationId]);

    // Load messages when conversationId changes (but only if not currently sending a message).
    // Note: We intentionally omit apiBaseUrl from deps and use apiBaseUrlRef instead.
    // When ApiConfigProvider's refreshSettings() updates apiBaseUrl, that would otherwise
    // re-run this effect and race with an in-flight loadMessages, causing "reload" flicker.
    useEffect(() => {
        const abortController = new AbortController();
        const signal = abortController.signal;

        // Skip reload if this is a conversation we just created
        if (conversationId === justCreatedConversationId.current) {
            justCreatedConversationId.current = null; // Clear the flag
            setCurrentConversationId(conversationId);
            return;
        }

        // New Chat: conversationId is null — reset right panel to empty state so user can start fresh
        if (conversationId === null) {
            setCurrentConversationId(null);
            setMessages([]);
            setError(null);
            setExecutionDetailsMessageId(null); // Close execution details modal
            return;
        }

        // Only reload if conversationId actually changed from outside (user selected different conversation)
        // and we're not in the middle of sending a message.
        if (conversationId !== currentConversationId && !isSendingMessage.current) {
            setCurrentConversationId(conversationId);
            loadMessages(conversationId, { signal });
        } else if (conversationId === currentConversationId && conversationId !== null && messages.length === 0 && !isSendingMessage.current) {
            // If conversationId matches but we have no messages, load them (initial load)
            loadMessages(conversationId, { signal });
        }

        return () => abortController.abort();
        // eslint-disable-next-line react-hooks/exhaustive-deps -- apiBaseUrl omitted intentionally; loadMessages uses apiBaseUrlRef
    }, [conversationId]);

    // Close execution details modal on Escape
    useEffect(() => {
        const onKeyDown = (e: KeyboardEvent) => {
            if (e.key === 'Escape' && executionDetailsMessageId != null) {
                setExecutionDetailsMessageId(null);
            }
        };
        window.addEventListener('keydown', onKeyDown);
        return () => window.removeEventListener('keydown', onKeyDown);
    }, [executionDetailsMessageId]);

    // Fetch execution details when user opens modal (clicks icon on assistant message).
    // Use conversationId (prop from URL) rather than currentConversationId so we fetch even when
    // internal state hasn't synced yet (e.g. after remount).
    useEffect(() => {
        if (executionDetailsMessageId == null || conversationId == null) {
            setExecutionDetailsData(null);
            setExecutionDetailsError(null);
            setExecutionDetailsLoading(false);
            return;
        }
        let cancelled = false;
        setExecutionDetailsLoading(true);
        setExecutionDetailsError(null);
        fetch(
            `${apiBaseUrl}/api/conversations/${conversationId}/messages/${executionDetailsMessageId}/execution-details`
        )
            .then((res) => {
                if (cancelled) return;
                if (!res.ok) {
                    if (res.status === 404) return { _noDetails: true };
                    throw new Error(res.statusText || 'Failed to load execution details');
                }
                return res.json();
            })
            .then((data) => {
                if (cancelled) return;
                setExecutionDetailsLoading(false);
                if (data && (data as any)._noDetails) {
                    setExecutionDetailsData(null);
                    setExecutionDetailsError('Execution details were not recorded for this message. This can happen for older messages or when metadata was not saved.');
                    return;
                }
                setExecutionDetailsData(data as any);
                setExecutionDetailsError(null);
            })
            .catch((err) => {
                if (!cancelled) {
                    setExecutionDetailsLoading(false);
                    setExecutionDetailsError(err instanceof Error ? err.message : 'Failed to load execution details');
                    setExecutionDetailsData(null);
                }
            });
        return () => {
            cancelled = true;
        };
    }, [executionDetailsMessageId, conversationId, apiBaseUrl]);

    // Render Mermaid graph when execution details include graph_mermaid
    useEffect(() => {
        const mermaidSrc = executionDetailsData?.graph_mermaid;
        const container = graphMermaidRef.current;
        if (!mermaidSrc || !container) return;
        try {
            mermaid.initialize({ startOnLoad: false });
            container.textContent = mermaidSrc;
            container.classList.add('mermaid');
            const run = async () => {
                try {
                    // @ts-ignore - suppressErrors is a valid runtime option
                    await mermaid.run({ nodes: [container], suppressErrors: true });
                } catch {
                    container.classList.remove('mermaid');
                    container.innerHTML = '';
                    const pre = document.createElement('pre');
                    pre.className = 'text-xs text-gray-500 dark:text-gray-400 p-2';
                    pre.textContent = 'Could not render graph.';
                    container.appendChild(pre);
                }
            };
            run();
        } catch {
            container.textContent = 'Could not render graph.';
        }
        return () => {
            container.textContent = '';
            container.classList.remove('mermaid');
            container.innerHTML = '';
        };
    }, [executionDetailsData?.graph_mermaid, executionDetailsMessageId]);

    const loadMessages = async (
        convId: number | null,
        options?: { keepStreamedAssistantIfMissing?: boolean; signal?: AbortSignal }
    ) => {
        if (!convId) {
            setMessages([]);
            setError(null);
            return;
        }

        try {
            setMessagesLoading(true);
            setError(null);
            const base = apiBaseUrlRef.current;
            const response = await fetch(`${base}/api/conversations/${convId}/messages`, {
                signal: options?.signal,
            });
            if (options?.signal?.aborted) return;
            if (response.ok) {
                const dbMessages = await response.json();
                if (options?.signal?.aborted) return;
                const formattedMessages = dbMessages.map((msg: any) => ({
                    id: `db-${msg.id}`,
                    role: msg.role,
                    content: msg.content,
                    createdAt: msg.created_at ? new Date(msg.created_at) : new Date(),
                    attachments: Array.isArray(msg.attachments) ? msg.attachments : [],
                    model_id: msg.model_id ?? undefined,
                }));
                if (options?.keepStreamedAssistantIfMissing) {
                    setMessages((prev) => {
                        if (formattedMessages.length >= prev.length) return formattedMessages;
                        const last = prev[prev.length - 1];
                        if (
                            last?.role === 'assistant' &&
                            last?.content != null &&
                            String(last.content).trim() !== '' &&
                            prev.length === formattedMessages.length + 1
                        ) {
                            return [...formattedMessages, last];
                        }
                        return formattedMessages;
                    });
                } else {
                    setMessages(formattedMessages);
                }
            } else {
                setError(new Error(`Failed to load messages (${response.status})`));
            }
        } catch (err) {
            if (err instanceof Error && err.name === 'AbortError') return;
            console.error("Error loading messages:", err);
            setError(err instanceof Error ? err : new Error("Failed to load messages"));
        } finally {
            setMessagesLoading(false);
        }
    };

    // Form submission handler
    const onSubmit = async (e: React.FormEvent<HTMLFormElement>) => {
        e.preventDefault();
        const message = inputValue.trim();

        if (!message || isLoading) {
            return;
        }

        console.log("Form submitted with message:", message);
        console.log("Current messages count:", messages.length);

        // Clear local input immediately
        setInputValue('');

        // Always use manual API call to ensure it goes to the correct backend
        console.log("Using manual API call to", apiBaseUrl + "/api/chat");
        setIsLoading(true);
        setError(null);
        isSendingMessage.current = true; // Mark that we're sending a message

        let activeConversationId: number | null = currentConversationId;
        const startedWithoutConversation = !activeConversationId;

        try {
            // If no conversation exists, create one first
            if (!activeConversationId) {
                const convResponse = await fetch(`${apiBaseUrl}/api/conversations`, {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                    },
                    body: JSON.stringify({
                        title: 'New Conversation',
                        user_name: 'User',
                    }),
                });

                if (convResponse.ok) {
                    const newConv = await convResponse.json();
                    activeConversationId = newConv.id as number;
                    setCurrentConversationId(activeConversationId);
                    // Mark this as a conversation we just created to prevent immediate reload
                    justCreatedConversationId.current = activeConversationId;
                } else {
                    throw new Error('Failed to create conversation');
                }
            }

            // At this point, activeConversationId should be set
            if (!activeConversationId) {
                throw new Error('No conversation ID available');
            }

            // TypeScript guard: activeConversationId is now definitely a number
            const convId: number = activeConversationId;
            const shouldRefreshConversationsSoon =
                startedWithoutConversation || justCreatedConversationId.current === convId;

            // Add user message to UI immediately using functional update
            const userMessage = {
                id: `user-${Date.now()}`,
                role: 'user' as const,
                content: message,
                createdAt: new Date(),
            };

            // Get current messages for API request
            const currentMessages = messages || [];

            // Add user message to UI immediately
            setMessages((prev) => [...prev, userMessage]);

            // Build last user message: text
            const lastUserMessage = { role: 'user', content: message };

            // Send message with conversation ID
            const response = await fetch(`${apiBaseUrl}/api/chat`, {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                },
                body: JSON.stringify({
                    conversation_id: convId,
                    messages: [
                        ...currentMessages.map((m: any) => ({
                            role: m.role,
                            content: m.content,
                        })),
                        lastUserMessage,
                    ],
                    web_search_enabled: false,
                    model_id: 'openai.gpt-4o',
                }),
            });

            if (!response.ok) {
                const errorText = await response.text();
                console.error("API Error:", response.status, errorText);
                throw new Error(`HTTP error! status: ${response.status}, message: ${errorText.substring(0, 200)}`);
            }

            // Handle streaming response
            const reader = response.body?.getReader();
            const decoder = new TextDecoder();

            if (reader) {
                let assistantMessage = "";
                const assistantMessageId = `assistant-${Date.now()}`;
                // Create initial assistant message (include model_id so UI can show it before loadMessages)
                const initialAssistantMessage = {
                    id: assistantMessageId,
                    role: 'assistant' as const,
                    content: "",
                    createdAt: new Date(),
                    model_id: 'openai.gpt-4o',
                };

                // Add empty assistant message using functional update to ensure we have the user message
                setMessages((prev) => [...prev, initialAssistantMessage]);

                while (true) {
                    const { done, value } = await reader.read();
                    if (done) {
                        break;
                    }

                    const chunk = decoder.decode(value);
                    assistantMessage += chunk;

                    // Update assistant message in real-time using functional update
                    setMessages((prev: any[]) => {
                        // Find and update the assistant message
                        const assistantIndex = prev.findIndex((msg: any) => msg.id === assistantMessageId);
                        if (assistantIndex >= 0) {
                            // Update existing assistant message
                            const updated = [...prev];
                            updated[assistantIndex] = {
                                ...updated[assistantIndex],
                                content: assistantMessage,
                            };
                            return updated;
                        } else {
                            // Add new assistant message if not found (shouldn't happen, but safety check)
                            return [...prev, {
                                id: assistantMessageId,
                                role: 'assistant' as const,
                                content: assistantMessage,
                                createdAt: new Date(),
                                model_id: 'openai.gpt-4o',
                            }];
                        }
                    });

                    console.log("Received chunk:", chunk);
                }

                console.log("Full response:", assistantMessage);
                setIsLoading(false);
                isSendingMessage.current = false; // Mark that we're done sending

                // Refetch messages so the new assistant message gets its DB id (for execution-details icon)
                await loadMessages(activeConversationId, { keepStreamedAssistantIfMissing: true });


                if (shouldRefreshConversationsSoon) {
                    setTimeout(() => {
                        try {
                            refreshConversations?.();
                        } catch { }
                    }, 2500);
                }

                if (activeConversationId && activeConversationId !== conversationId) {
                    onConversationCreated(activeConversationId);
                }
            } else {
                console.error("Reader not available");
                setIsLoading(false);
                isSendingMessage.current = false;
            }
        } catch (err) {
            console.error("Error with manual API call:", err);
            setIsLoading(false);
            isSendingMessage.current = false;
            setError(err instanceof Error ? err : new Error(String(err)));

            // Show error in UI
            const errorMessage = {
                id: `error-${Date.now()}`,
                role: 'assistant' as const,
                content: `Error: ${err instanceof Error ? err.message : String(err)}`,
                createdAt: new Date(),
            };
            setMessages((prev: any[]) => [...prev, errorMessage]);
        }
    };

    // Debug: Log messages and loading state
    console.log("Current messages:", messages);
    console.log("Is loading:", isLoading);
    console.log("Error:", error);
    console.log("Input value:", inputValue);

    // Custom components for react-markdown
    const getMarkdownComponents = (currentMessage: any) => ({
        code: ({ node, inline, className, children, ...props }: any) => {
            const match = /language-(\w+)/.exec(className || '');
            const language = match ? match[1] : '';
            const codeString = String(children).replace(/\n$/, '');

            const clipboardSvg = (
                <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h2m8 0h2a2 2 0 012 2v2m2 4a2 2 0 01-2 2h-8a2 2 0 01-2-2v-8a2 2 0 012-2h2" />
                </svg>
            );

            // If it's an HTML code block, render in iframe instead of showing code
            if (language === 'html' && !inline) {
                const key = codeBlockCopyKey(codeString);
                return (
                    <div className="relative my-2">
                        <div className="absolute top-2 right-2 z-10">
                            <button
                                type="button"
                                onClick={() => copyCodeBlock(codeString)}
                                className="p-1.5 rounded bg-gray-600 hover:bg-gray-500 text-gray-200 hover:text-white transition-colors"
                                title="Copy to clipboard"
                                aria-label="Copy code"
                            >
                                {copiedCodeBlockKey === key ? <span className="text-xs">Copied!</span> : clipboardSvg}
                            </button>
                        </div>
                        <ArtifactRenderer html={codeString} />
                    </div>
                );
            }

            // If it's a Vega-Lite spec: try parsing any non-inline code block as JSON; render if $schema contains vega-lite
            // Also intercept sandbox-table JSON markers for PaginatedTable
            if (!inline) {
                try {
                    // Strip JS-style comments the LLM sometimes emits (// ... and /* ... */)
                    // but preserve // inside quoted strings (e.g. "https://...")
                    const stripped = codeString
                        .replace(/"(?:[^"\\]|\\.)*"|\/\/[^\n]*/g, (m) => m.startsWith('"') ? m : '')
                        .replace(/\/\*[\s\S]*?\*\//g, ''); // block comments
                    const parsed = JSON.parse(stripped) as Record<string, unknown>;

                    // Sandbox paginated table
                    if (parsed?._sandboxTable === true) {
                        return (
                            <PaginatedTable
                                columns={parsed.columns as string[]}
                                data={parsed.data as Record<string, unknown>[]}
                                pageSize={20}
                            />
                        );
                    }

                    const schema = typeof parsed?.$schema === 'string' ? parsed.$schema : '';
                    if (schema.includes('vega-lite')) {
                        const key = codeBlockCopyKey(codeString);
                        return (
                            <div className="relative my-2">
                                <div className="absolute top-2 right-2 z-10">
                                    <button
                                        type="button"
                                        onClick={() => copyCodeBlock(codeString)}
                                        className="p-1.5 rounded bg-gray-600 hover:bg-gray-500 text-gray-200 hover:text-white transition-colors"
                                        title="Copy to clipboard"
                                        aria-label="Copy code"
                                    >
                                        {copiedCodeBlockKey === key ? <span className="text-xs">Copied!</span> : clipboardSvg}
                                    </button>
                                </div>
                                <VegaLiteEmbed spec={parsed} />
                            </div>
                        );
                    }
                } catch {
                    // Not valid JSON or not Vega-Lite; fall through to default code block
                }

            }

            // Default code block rendering for inline code
            if (inline) {
                return (
                    <code className="px-1.5 py-0.5 bg-gray-200 dark:bg-gray-700 rounded text-sm" {...props}>
                        {children}
                    </code>
                );
            }



            // Code block rendering (lighter dark gray for SQL/code blocks) with copy button
            const key = codeBlockCopyKey(codeString);
            return (
                <div className="relative my-2">
                    <div className="absolute top-2 right-2 z-10">
                        <button
                            type="button"
                            onClick={() => copyCodeBlock(codeString)}
                            className="p-1.5 rounded bg-gray-600 hover:bg-gray-500 text-gray-200 hover:text-white transition-colors"
                            title="Copy to clipboard"
                            aria-label="Copy code"
                        >
                            {copiedCodeBlockKey === key ? <span className="text-xs">Copied!</span> : clipboardSvg}
                        </button>
                    </div>
                    <pre className="bg-gray-700 dark:bg-gray-800 rounded-lg p-4 pr-12 overflow-x-auto my-2">
                        <code className="text-sm text-gray-100" {...props}>
                            {children}
                        </code>
                    </pre>
                </div>
            );
        },
        p: ({ node, children, ...props }: any) => {
            // Code blocks (<pre>) cannot be nested inside <p> tags per HTML spec — use div when any descendant is block-level code
            const hasBlockCodeInAst = node?.children?.some((child: any) =>
                child.type === 'code' || child.tagName === 'code' || child.tagName === 'pre'
            );

            const hasPreInRenderedChildren = (elements: React.ReactNode): boolean => {
                return React.Children.toArray(elements).some((child: any) => {
                    if (!React.isValidElement(child)) return false;
                    if (child.type === 'pre') return true;
                    if (child.type === React.Fragment) return hasPreInRenderedChildren((child.props as { children?: React.ReactNode })?.children);
                    const props = child.props as { className?: string; children?: React.ReactNode };
                    if (typeof props?.className === 'string' && (props.className.includes('bg-gray-700') || props.className.includes('bg-gray-800'))) return true;
                    return hasPreInRenderedChildren(props?.children ?? []);
                });
            };

            if (hasBlockCodeInAst || hasPreInRenderedChildren(children)) {
                return <div className="mb-2 last:mb-0">{children}</div>;
            }
            return <p className="mb-2 last:mb-0" {...props}>{children}</p>;
        },
        ul: ({ children }: any) => <ul className="list-disc list-inside mb-2 space-y-1">{children}</ul>,
        ol: ({ children }: any) => <ol className="list-decimal list-inside mb-2 space-y-1">{children}</ol>,
        li: ({ children }: any) => <li className="ml-4">{children}</li>,
        h1: ({ children }: any) => <h1 className="text-2xl font-bold mb-2 mt-4 first:mt-0">{children}</h1>,
        h2: ({ children }: any) => <h2 className="text-xl font-bold mb-2 mt-4 first:mt-0">{children}</h2>,
        h3: ({ children }: any) => <h3 className="text-lg font-bold mb-2 mt-4 first:mt-0">{children}</h3>,
        blockquote: ({ children }: any) => (
            <blockquote className="border-l-4 border-gray-300 dark:border-gray-600 pl-4 italic my-2">
                {children}
            </blockquote>
        ),
        table: ({ children }: any) => (
            <div className="my-3 overflow-x-auto">
                <table className="min-w-full border border-gray-300 dark:border-gray-600 border-collapse text-sm">
                    {children}
                </table>
            </div>
        ),
        thead: ({ children }: any) => <thead className="bg-gray-100 dark:bg-gray-800">{children}</thead>,
        tbody: ({ children }: any) => <tbody>{children}</tbody>,
        tr: ({ children }: any) => (
            <tr className="border-b border-gray-200 dark:border-gray-700">{children}</tr>
        ),
        th: ({ children }: any) => (
            <th className="border border-gray-300 dark:border-gray-600 px-3 py-2 text-left font-semibold text-gray-900 dark:text-gray-100">
                {children}
            </th>
        ),
        td: ({ children }: any) => (
            <td className="border border-gray-300 dark:border-gray-600 px-3 py-2 text-gray-800 dark:text-gray-200">
                {children}
            </td>
        ),
    });

    return (
        <div className="h-full w-full flex flex-col bg-app-bg">
            {/* Messages area */}
            <div className="flex-1 overflow-y-auto p-6 space-y-4">
                {messagesLoading && messages.length === 0 && (
                    <div className="space-y-4 max-w-[70%] mx-auto pt-8 animate-pulse">
                        {/* Skeleton: user message (right-aligned) */}
                        <div className="flex justify-end">
                            <div className="rounded-lg px-4 py-3 bg-app-user-bubble-bg/30 w-48">
                                <div className="h-4 bg-app-border/40 rounded w-full mb-2" />
                                <div className="h-4 bg-app-border/30 rounded w-2/3" />
                            </div>
                        </div>
                        {/* Skeleton: assistant message (left-aligned) */}
                        <div className="flex items-start gap-3">
                            <div className="flex-shrink-0 w-8 h-8 rounded-full bg-app-accent/30" />
                            <div className="rounded-lg px-4 py-3 bg-app-message-bg/30 w-72">
                                <div className="h-4 bg-app-border/40 rounded w-full mb-2" />
                                <div className="h-4 bg-app-border/30 rounded w-5/6 mb-2" />
                                <div className="h-4 bg-app-border/20 rounded w-1/2" />
                            </div>
                        </div>
                        {/* Skeleton: user message */}
                        <div className="flex justify-end">
                            <div className="rounded-lg px-4 py-3 bg-app-user-bubble-bg/30 w-36">
                                <div className="h-4 bg-app-border/40 rounded w-full" />
                            </div>
                        </div>
                    </div>
                )}
                {!messagesLoading && messages.length === 0 && (
                    <div className="flex items-center justify-center h-full">
                        <p className="text-gray-500 dark:text-gray-400">
                            Start a conversation by typing a message below.
                        </p>
                    </div>
                )}
                {messages.map((message, index) => {
                    // Format timestamp in user's local timezone (DB stores UTC; we parse as UTC then display local)
                    const formatMessageTimestamp = (date: Date | string) => {
                        const d = typeof date === 'string' ? new Date(date + (date.includes('Z') ? '' : 'Z')) : date;
                        const now = new Date();
                        const diffMs = now.getTime() - d.getTime();
                        const oneDayMs = 24 * 60 * 60 * 1000;
                        const hours = d.getHours().toString().padStart(2, '0');
                        const minutes = d.getMinutes().toString().padStart(2, '0');
                        const timeStr = `${hours}:${minutes}`;
                        if (diffMs >= oneDayMs) {
                            const monthNames = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
                            const day = d.getDate();
                            const month = monthNames[d.getMonth()];
                            const year = d.getFullYear();
                            const yearStr = year !== now.getFullYear() ? `, ${year}` : '';
                            return `${month} ${day}${yearStr}, ${timeStr}`;
                        }
                        return timeStr;
                    };

                    const timestamp = (message as any).createdAt
                        ? formatMessageTimestamp((message as any).createdAt)
                        : formatMessageTimestamp(new Date());

                    return (
                        <div
                            key={message.id || `msg-${index}`}
                            className={`flex items-start gap-3 group ${message.role === 'user' ? 'justify-end' : 'justify-start'
                                }`}
                        >
                            {message.role === 'assistant' && (
                                <div className="flex-shrink-0 w-8 h-8 rounded-full bg-app-accent flex items-center justify-center">
                                    <svg
                                        className="w-5 h-5 text-white"
                                        fill="none"
                                        stroke="currentColor"
                                        viewBox="0 0 24 24"
                                    >
                                        <path
                                            strokeLinecap="round"
                                            strokeLinejoin="round"
                                            strokeWidth={2}
                                            d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z"
                                        />
                                    </svg>
                                </div>
                            )}

                            <div className={`flex flex-col max-w-[70%] ${message.role === 'user' ? 'items-end' : 'items-start'
                                }`}>
                                <div className="flex items-center gap-2">
                                    <div
                                        data-message-role={message.role}
                                        className={`rounded-lg px-4 py-3 transition-all duration-200 ease-in-out hover:shadow-md hover:-translate-y-[1px] ${message.role === 'user'
                                            ? 'bg-app-user-bubble-bg text-app-user-bubble-text shadow-[var(--app-user-message-shadow)] border border-[var(--app-user-message-border)]'
                                            : 'bg-app-message-bg text-app-message-text shadow-[var(--app-message-shadow)] border border-[var(--app-message-border)]'
                                            }`}
                                    >
                                        {message.role === 'user' ? (
                                            <p className="text-sm whitespace-pre-wrap">{message.content}</p>
                                        ) : (
                                            <div className="text-sm">
                                                <ReactMarkdown remarkPlugins={[remarkGfm]} components={getMarkdownComponents(message)}>
                                                    {message.content || ''}
                                                </ReactMarkdown>
                                            </div>
                                        )}
                                    </div>
                                    {message.role === 'assistant' && typeof message.id === 'string' && message.id.startsWith('db-') && (
                                        <button
                                            type="button"
                                            onClick={() => setExecutionDetailsMessageId(parseInt(message.id.replace('db-', ''), 10))}
                                            className="flex-shrink-0 p-1.5 rounded bg-app-selected-bg text-app-text hover:opacity-90 opacity-0 group-hover:opacity-100 transition-opacity duration-200"
                                            title="View execution details"
                                            aria-label="View execution details"
                                        >
                                            <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2m-3 7h3m-3 4h3m-6-4h.01M9 16h.01" />
                                            </svg>
                                        </button>
                                    )}
                                </div>
                                <div className="flex items-center gap-2 mt-1 px-1">
                                    <button
                                        type="button"
                                        onClick={() => copyMessageContent(message.content ?? '', String(message.id ?? 'msg-' + index))}
                                        className="p-1.5 rounded text-app-text-muted hover:text-app-text hover:bg-app-surface transition-all duration-200 opacity-0 group-hover:opacity-100"
                                        title="Copy to clipboard"
                                        aria-label="Copy message"
                                    >
                                        {copiedMessageId === String(message.id ?? 'msg-' + index) ? (
                                            <span className="text-xs text-green-600 dark:text-green-400">Copied!</span>
                                        ) : (
                                            <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h2m8 0h2a2 2 0 012 2v2m2 4a2 2 0 01-2 2h-8a2 2 0 01-2-2v-8a2 2 0 012-2h2" />
                                            </svg>
                                        )}
                                    </button>
                                    <span className="text-xs text-app-text-muted">
                                        {timestamp}
                                    </span>

                                </div>
                            </div>

                            {message.role === 'user' && (
                                <div className="flex-shrink-0 w-8 h-8 rounded-full bg-app-accent flex items-center justify-center">
                                    <svg
                                        className="w-5 h-5 text-white"
                                        fill="none"
                                        stroke="currentColor"
                                        viewBox="0 0 24 24"
                                    >
                                        <path
                                            strokeLinecap="round"
                                            strokeLinejoin="round"
                                            strokeWidth={2}
                                            d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z"
                                        />
                                    </svg>
                                </div>
                            )}
                        </div>
                    );
                })}
                {isLoading && (
                    <div className="flex justify-start">
                        <div className="bg-app-message-bg rounded-lg px-4 py-2">
                            <div className="flex items-center gap-2">
                                <div className="animate-spin rounded-full h-4 w-4 border-b-2 border-black dark:border-white"></div>
                                <p className="text-sm text-black dark:text-white">Thinking...</p>
                            </div>
                        </div>
                    </div>
                )}
                {error && (
                    <div className="flex justify-start">
                        <div className="bg-red-100 dark:bg-red-900 rounded-lg px-4 py-2 max-w-[80%]">
                            <p className="text-sm text-red-700 dark:text-red-300">
                                Error: {error.message || String(error)}
                            </p>
                        </div>
                    </div>
                )}
            </div>

            <div className="border-t border-app-border p-3 bg-app-surface">
                <div className="max-w-4xl mx-auto">


                    <form
                        onSubmit={onSubmit}
                        className="group relative bg-app-bg border border-app-border focus-within:border-app-accent/50 focus-within:shadow-md focus-within:ring-1 focus-within:ring-app-accent/20 rounded-2xl transition-all duration-200"
                    >
                        {/* Input Area */}
                        <div className="p-2">
                            <textarea
                                ref={(el) => {
                                    // We can't attach ref directly if we want to auto-resize, but for now standard textarea is fine.
                                    // Let's use a simple auto-resize approach or just standard textarea
                                }}
                                value={inputValue}
                                onChange={onInputChange}
                                onKeyDown={(e) => {
                                    if (e.key === 'Enter' && !e.shiftKey) {
                                        e.preventDefault();
                                        onSubmit(e as any);
                                    }
                                    // Allow auto-resize logic if needed, but for simplicity relying on rows or CSS
                                    const target = e.target as HTMLTextAreaElement;
                                    target.style.height = 'auto';
                                    target.style.height = `${Math.min(target.scrollHeight, 200)}px`;
                                }}
                                onInput={(e) => {
                                    const target = e.target as HTMLTextAreaElement;
                                    target.style.height = 'auto';
                                    target.style.height = `${Math.min(target.scrollHeight, 200)}px`;
                                }}

                                placeholder="Type your message..."
                                disabled={isLoading}
                                rows={1}
                                className="w-full max-h-[200px] resize-none border-0 bg-transparent p-1 text-app-text placeholder-app-text-muted focus:ring-0 focus:outline-none text-base leading-relaxed"
                                style={{ minHeight: '40px' }}
                            />
                        </div>

                        {/* Toolbar Area */}
                        <div className="flex items-center justify-between px-2 pb-2">
                            <div className="flex items-center gap-1">


                            </div>

                            {/* Send Button */}
                            <button
                                type="submit"
                                disabled={isLoading || !inputValue.trim()}
                                className="flex items-center justify-center w-8 h-8 rounded-full bg-app-text text-app-bg hover:opacity-90 disabled:opacity-30 disabled:cursor-not-allowed transition-all shadow-sm"
                                title="Send message"
                            >
                                {isLoading ? (
                                    <div className="w-4 h-4 border-2 border-app-bg border-t-transparent rounded-full animate-spin" />
                                ) : (
                                    <svg className="w-4 h-4 ml-0.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2.5} d="M5 12h14M12 5l7 7-7 7" />
                                    </svg>
                                )}
                            </button>
                        </div>
                    </form>

                    <div className="text-center mt-2.5">
                        <p className="text-[10px] text-app-text-muted/60">
                            AI can make mistakes. Please check important information.
                        </p>
                    </div>
                </div>
            </div>

            {/* Execution details modal */}
            {executionDetailsMessageId != null && (
                <div
                    className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
                    onClick={() => setExecutionDetailsMessageId(null)}
                    onKeyDown={(e) => e.key === 'Escape' && setExecutionDetailsMessageId(null)}
                    role="dialog"
                    aria-modal="true"
                    aria-labelledby="execution-details-title"
                >
                    <div
                        className="bg-app-surface rounded-lg shadow-xl max-w-2xl w-full max-h-[85vh] overflow-hidden flex flex-col"
                        onClick={(e) => e.stopPropagation()}
                    >
                        <div className="flex items-center justify-between px-4 py-3 border-b border-app-border">
                            <h2 id="execution-details-title" className="text-lg font-semibold text-app-text">
                                Execution details
                            </h2>
                            <button
                                type="button"
                                onClick={() => setExecutionDetailsMessageId(null)}
                                className="p-1 rounded text-app-text-muted hover:text-app-text hover:bg-app-surface"
                                aria-label="Close"
                            >
                                <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                                </svg>
                            </button>
                        </div>
                        <div className="overflow-y-auto flex-1 p-4 space-y-4">
                            {executionDetailsLoading && (
                                <div className="flex items-center gap-2 text-gray-500 dark:text-gray-400">
                                    <div className="animate-spin rounded-full h-5 w-5 border-b-2 border-gray-600 dark:border-gray-400" />
                                    <span>Loading execution details...</span>
                                </div>
                            )}
                            {executionDetailsError && (
                                <p className="text-sm text-red-600 dark:text-red-400">{executionDetailsError}</p>
                            )}
                            {!executionDetailsLoading && !executionDetailsError && !executionDetailsData && executionDetailsMessageId != null && (
                                <p className="text-sm text-gray-500 dark:text-gray-400">
                                    {conversationId == null
                                        ? 'Unable to load: no conversation selected.'
                                        : 'No execution details available for this message.'}
                                </p>
                            )}
                            {!executionDetailsLoading && !executionDetailsError && executionDetailsData && (
                                <>
                                    {(executionDetailsData as any).error && (
                                        <section>
                                            <h3 className="text-sm font-semibold text-red-600 dark:text-red-400 mb-2">
                                                Error
                                            </h3>
                                            <pre className="rounded border border-red-200 dark:border-red-800 p-3 text-xs overflow-x-auto max-h-64 overflow-y-auto bg-red-50 dark:bg-red-950/30 text-red-800 dark:text-red-200 whitespace-pre-wrap break-words">
                                                {(executionDetailsData as any).error}
                                            </pre>
                                            {(executionDetailsData as any).error_type && (
                                                <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
                                                    Type: {(executionDetailsData as any).error_type}
                                                </p>
                                            )}
                                        </section>
                                    )}
                                    <section>
                                        <h3 className="text-sm font-semibold text-gray-700 dark:text-gray-300 mb-2">
                                            Prompt (context sent to the agent)
                                        </h3>
                                        <div className="space-y-2 text-sm">
                                            {executionDetailsData.prompt_messages?.map((pm: any, i: number) => (
                                                <div key={i} className="rounded border border-gray-200 dark:border-gray-700 p-3">
                                                    <span className="font-medium text-gray-600 dark:text-gray-400">{pm.role}</span>
                                                    <pre className="mt-1 whitespace-pre-wrap break-words text-gray-900 dark:text-gray-100 font-sans">
                                                        {sanitizeExecutionPromptContent(pm.content) || '(empty)'}
                                                    </pre>
                                                    {pm.tool_calls?.length > 0 && (
                                                        <div className="mt-2 text-xs text-gray-500 dark:text-gray-400">
                                                            Tool calls: {pm.tool_calls.map((tc: any) => tc.name).join(', ')}
                                                        </div>
                                                    )}
                                                </div>
                                            ))}
                                        </div>
                                    </section>
                                    {executionDetailsData.graph_mermaid && (
                                        <section>
                                            <h3 className="text-sm font-semibold text-gray-700 dark:text-gray-300 mb-2">
                                                Agent graph
                                            </h3>
                                            <div
                                                ref={graphMermaidRef}
                                                className="rounded border border-gray-200 dark:border-gray-700 p-3 bg-gray-50 dark:bg-gray-800 overflow-x-auto min-h-[120px]"
                                            />
                                        </section>
                                    )}
                                    {executionDetailsData.full_prompt != null && (
                                        <section>
                                            <div className="flex items-center justify-between gap-2 mb-2">
                                                <h3 className="text-sm font-semibold text-gray-700 dark:text-gray-300">
                                                    Full prompt sent to LLM
                                                </h3>
                                                <button
                                                    type="button"
                                                    onClick={() => copyExecutionSection(
                                                        typeof executionDetailsData.full_prompt === 'object'
                                                            ? JSON.stringify(executionDetailsData.full_prompt, null, 2)
                                                            : String(executionDetailsData.full_prompt),
                                                        'full_prompt'
                                                    )}
                                                    className="p-1.5 rounded text-gray-500 hover:text-gray-700 hover:bg-gray-200 dark:hover:bg-gray-700 dark:text-gray-400 dark:hover:text-gray-200"
                                                    title="Copy to clipboard"
                                                    aria-label="Copy full prompt to clipboard"
                                                >
                                                    {copiedSection === 'full_prompt' ? (
                                                        <span className="text-xs text-green-600 dark:text-green-400">Copied!</span>
                                                    ) : (
                                                        <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                                            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h2m8 0h2a2 2 0 012 2v2m2 4a2 2 0 01-2 2h-8a2 2 0 01-2-2v-8a2 2 0 012-2h2" />
                                                        </svg>
                                                    )}
                                                </button>
                                            </div>
                                            <pre className="rounded border border-gray-200 dark:border-gray-700 p-3 text-xs overflow-x-auto max-h-64 overflow-y-auto bg-gray-50 dark:bg-gray-800 text-gray-900 dark:text-gray-100 whitespace-pre-wrap break-words">
                                                {typeof executionDetailsData.full_prompt === 'object'
                                                    ? JSON.stringify(executionDetailsData.full_prompt, null, 2)
                                                    : String(executionDetailsData.full_prompt)}
                                            </pre>
                                        </section>
                                    )}
                                    <section>
                                        <div className="flex items-center justify-between gap-2 mb-2">
                                            <h3 className="text-sm font-semibold text-gray-700 dark:text-gray-300">
                                                Tool invocations
                                            </h3>
                                            <button
                                                type="button"
                                                onClick={() => copyExecutionSection(
                                                    executionDetailsData.tool_calls?.length
                                                        ? JSON.stringify(
                                                            [...(executionDetailsData.tool_calls || [])].sort((a: any, b: any) => (a.sequence ?? 0) - (b.sequence ?? 0)),
                                                            null,
                                                            2
                                                        )
                                                        : 'No tools were invoked.',
                                                    'tool_invocations'
                                                )}
                                                className="p-1.5 rounded text-gray-500 hover:text-gray-700 hover:bg-gray-200 dark:hover:bg-gray-700 dark:text-gray-400 dark:hover:text-gray-200"
                                                title="Copy to clipboard"
                                                aria-label="Copy tool invocations to clipboard"
                                            >
                                                {copiedSection === 'tool_invocations' ? (
                                                    <span className="text-xs text-green-600 dark:text-green-400">Copied!</span>
                                                ) : (
                                                    <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h2m8 0h2a2 2 0 012 2v2m2 4a2 2 0 01-2 2h-8a2 2 0 01-2-2v-8a2 2 0 012-2h2" />
                                                    </svg>
                                                )}
                                            </button>
                                        </div>
                                        {executionDetailsData.tool_calls?.length ? (
                                            <div className="space-y-3">
                                                {executionDetailsData.tool_calls
                                                    .sort((a: any, b: any) => (a.sequence ?? 0) - (b.sequence ?? 0))
                                                    .map((tc: any, i: number) => (
                                                        <div key={i} className="rounded border border-gray-200 dark:border-gray-700 p-3">
                                                            <div className="font-medium text-gray-800 dark:text-gray-200">
                                                                {tc.sequence}. {tc.name}
                                                            </div>
                                                            <div className="mt-1 text-xs text-gray-500 dark:text-gray-400 flex flex-wrap gap-x-4 gap-y-0.5">
                                                                <span>Invoked at: {tc.invoked_at ?? '—'}</span>
                                                                <span>Duration: {tc.duration_ms != null ? `${tc.duration_ms} ms` : '—'}</span>
                                                            </div>
                                                            <div className="mt-1 text-xs text-gray-600 dark:text-gray-400">
                                                                Arguments:{' '}
                                                                <pre className="inline whitespace-pre-wrap break-all">
                                                                    {typeof tc.args === 'object' ? JSON.stringify(tc.args, null, 2) : String(tc.args)}
                                                                </pre>
                                                            </div>
                                                            <div className="mt-2 text-xs">
                                                                <span className="text-gray-500 dark:text-gray-400">Output:</span>
                                                                <pre className="mt-0.5 whitespace-pre-wrap break-words text-gray-900 dark:text-gray-100">
                                                                    {tc.output ?? '(empty)'}
                                                                </pre>
                                                            </div>
                                                        </div>
                                                    ))}
                                            </div>
                                        ) : (
                                            <p className="text-sm text-gray-500 dark:text-gray-400">No tools were invoked.</p>
                                        )}
                                    </section>
                                    {executionDetailsData.raw_response != null && (
                                        <section>
                                            <div className="flex items-center justify-between gap-2 mb-2">
                                                <h3 className="text-sm font-semibold text-gray-700 dark:text-gray-300">
                                                    Raw LLM response
                                                </h3>
                                                <button
                                                    type="button"
                                                    onClick={() => copyExecutionSection(
                                                        typeof executionDetailsData.raw_response === 'object'
                                                            ? JSON.stringify(executionDetailsData.raw_response, null, 2)
                                                            : String(executionDetailsData.raw_response),
                                                        'raw_response'
                                                    )}
                                                    className="p-1.5 rounded text-gray-500 hover:text-gray-700 hover:bg-gray-200 dark:hover:bg-gray-700 dark:text-gray-400 dark:hover:text-gray-200"
                                                    title="Copy to clipboard"
                                                    aria-label="Copy raw LLM response to clipboard"
                                                >
                                                    {copiedSection === 'raw_response' ? (
                                                        <span className="text-xs text-green-600 dark:text-green-400">Copied!</span>
                                                    ) : (
                                                        <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                                            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h2m8 0h2a2 2 0 012 2v2m2 4a2 2 0 01-2 2h-8a2 2 0 01-2-2v-8a2 2 0 012-2h2" />
                                                        </svg>
                                                    )}
                                                </button>
                                            </div>
                                            <pre className="rounded border border-gray-200 dark:border-gray-700 p-3 text-xs overflow-x-auto max-h-64 overflow-y-auto bg-gray-50 dark:bg-gray-800 text-gray-900 dark:text-gray-100 whitespace-pre-wrap break-words">
                                                {typeof executionDetailsData.raw_response === 'object'
                                                    ? JSON.stringify(executionDetailsData.raw_response, null, 2)
                                                    : String(executionDetailsData.raw_response)}
                                            </pre>
                                        </section>
                                    )}
                                    <section>
                                        <div className="flex items-center justify-between gap-2 mb-2">
                                            <h3 className="text-sm font-semibold text-gray-700 dark:text-gray-300">
                                                Final response
                                            </h3>
                                            <button
                                                type="button"
                                                onClick={() => {
                                                    const msg = messages.find((m: any) => m.id === `db-${executionDetailsMessageId}`);
                                                    copyExecutionSection(msg?.content ?? '(same as message above)', 'final_response');
                                                }}
                                                className="p-1.5 rounded text-gray-500 hover:text-gray-700 hover:bg-gray-200 dark:hover:bg-gray-700 dark:text-gray-400 dark:hover:text-gray-200"
                                                title="Copy to clipboard"
                                                aria-label="Copy final response to clipboard"
                                            >
                                                {copiedSection === 'final_response' ? (
                                                    <span className="text-xs text-green-600 dark:text-green-400">Copied!</span>
                                                ) : (
                                                    <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h2m8 0h2a2 2 0 012 2v2m2 4a2 2 0 01-2 2h-8a2 2 0 01-2-2v-8a2 2 0 012-2h2" />
                                                    </svg>
                                                )}
                                            </button>
                                        </div>
                                        <div className="rounded border border-gray-200 dark:border-gray-700 p-3 text-sm text-gray-900 dark:text-gray-100">
                                            {(() => {
                                                const msg = messages.find((m: any) => m.id === `db-${executionDetailsMessageId}`);
                                                return msg?.content ? (
                                                    <div>
                                                        <ReactMarkdown remarkPlugins={[remarkGfm]} components={getMarkdownComponents(msg)}>{msg.content}</ReactMarkdown>
                                                    </div>
                                                ) : (
                                                    <span className="text-gray-500 dark:text-gray-400">(same as message above)</span>
                                                );
                                            })()}
                                        </div>
                                    </section>
                                </>
                            )}
                        </div>
                    </div>
                </div>
            )}

        </div>
    );
}
