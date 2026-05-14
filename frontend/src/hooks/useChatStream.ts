import { useState, useRef, useCallback, type Dispatch, type SetStateAction, type MutableRefObject } from 'react';
import { CHAT_SESSION_ID_STORAGE_KEY } from '../api/createChatSession';
import { streamMessageBodySchema } from '../api/schemas';
import { apiUrl } from '../lib/apiBase';

export type StreamEventType = 'loading' | 'chunk' | 'options' | 'complete';

export type Message = {
    role: 'user' | 'bot';
    content: string;
    /** From API history or voice/commit — show mic affordance when `voice` */
    source?: 'text' | 'voice';
    /** From `GET …/sessions/{id}` or `voice/commit` for stable list keys */
    serverMessageId?: string;
    options?: string[];
    /** Local-only rows updated during OpenAI Realtime; removed when POST …/voice/commit succeeds */
    optimisticVoice?: boolean;
    /** Stable id for one local voice turn, used to reconcile commit responses without touching other turns */
    voiceClientTurnId?: string;
    /** Bot row: correlates `response.output_audio_transcript.*` to this bubble */
    voiceResponseId?: string;
    /** Bot row: cumulative transcript text per OpenAI response in the same voice turn */
    voiceAssistantResponseTexts?: Record<string, string>;
    /** User: correlates input-audio transcription deltas to this bubble */
    voiceItemId?: string;
    /** Bot: show streaming cursor until transcript `done` */
    voiceAssistantStreaming?: boolean;
    /** User: show streaming cursor while partial STT streams */
    voiceUserStreaming?: boolean;
};

export interface StreamEvent {
    type: StreamEventType;
    content?: string;
    options?: string[];
}

interface UseChatStreamResult {
    messages: Message[];
    status: 'idle' | 'loading' | 'streaming' | 'complete' | 'error';
    loadingText: string | null;
    sendMessage: (
        msg: string,
        resetContext: boolean,
        authToken: string,
        sessionId?: string | null,
        onInit?: (sessionId?: string) => void,
        /** Fired once when SSE `done` includes a non-empty `session_title` (first assistant reply naming). */
        onSessionTitle?: (title: string) => void
    ) => Promise<void>;
    resetChat: () => void;
    options: string[] | null;
    setMessages: Dispatch<SetStateAction<Message[]>>;
    setStatus: Dispatch<SetStateAction<'idle' | 'loading' | 'streaming' | 'complete' | 'error'>>;
}

export function useChatStream(): UseChatStreamResult {
    const [messages, setMessages] = useState<Message[]>([]);
    const [status, setStatus] = useState<'idle' | 'loading' | 'streaming' | 'complete' | 'error'>('idle');
    const [loadingText, setLoadingText] = useState<string | null>(null);
    const [options, setOptions] = useState<string[] | null>(null);

    // Ref to accumulate absolute bot message content during streaming
    const currentBotMessage = useRef<string>('');
    const onSessionTitleRef: MutableRefObject<((title: string) => void) | undefined> = useRef(undefined);

    const resetChat = useCallback(() => {
        setMessages([]);
        setStatus('idle');
        setOptions(null);
        setLoadingText(null);
        currentBotMessage.current = '';
    }, []);

    const handleEvent = useCallback((event: Record<string, unknown>) => {
        const type = (event.type ?? event.event) as string | undefined;
        const content =
            (typeof event.content === 'string' ? event.content : '') ||
            (typeof event.text === 'string' ? event.text : '') ||
            (typeof event.message === 'string' ? event.message : '');
        const options =
            (Array.isArray(event.options) ? (event.options as string[]) : null) ||
            (Array.isArray(event.suggestions) ? (event.suggestions as string[]) : null);

        switch (type) {
            case 'loading':
                if (content) {
                    setLoadingText(content);
                }
                break;
            case 'chunk':
            case 'delta':
                if (content) {
                    currentBotMessage.current += content;
                    setMessages((prev) => {
                        const newArr = [...prev];
                        if (newArr.length === 0) return newArr;
                        const lastMsg = newArr[newArr.length - 1];
                        if (lastMsg && lastMsg.role === 'bot') {
                            newArr[newArr.length - 1] = {
                                ...lastMsg,
                                content: currentBotMessage.current
                            };
                        }
                        return newArr;
                    });
                }
                break;
            case 'done': {
                const doneMsg = typeof event.message === 'string' ? event.message : '';
                if (doneMsg.length > 0) {
                    currentBotMessage.current = doneMsg;
                    setMessages((prev) => {
                        const newArr = [...prev];
                        if (newArr.length === 0) return newArr;
                        const lastMsg = newArr[newArr.length - 1];
                        if (lastMsg && lastMsg.role === 'bot') {
                            newArr[newArr.length - 1] = {
                                ...lastMsg,
                                content: currentBotMessage.current
                            };
                        }
                        return newArr;
                    });
                }
                const sessionTitle = event.session_title;
                if (typeof sessionTitle === 'string' && sessionTitle.trim().length > 0) {
                    onSessionTitleRef.current?.(sessionTitle.trim());
                }
                setLoadingText(null);
                break;
            }
            case 'options':
                if (options) {
                    setOptions(options);
                    setMessages((prev) => {
                        const newArr = [...prev];
                        if (newArr.length === 0) return newArr;
                        const lastMsg = newArr[newArr.length - 1];
                        if (lastMsg && lastMsg.role === 'bot') {
                            newArr[newArr.length - 1] = {
                                ...lastMsg,
                                options: options
                            };
                        }
                        return newArr;
                    });
                }
                break;
            case 'complete':
                break;
            case 'error': {
                const detail =
                    typeof event.detail === 'string'
                        ? event.detail
                        : event.detail != null
                          ? JSON.stringify(event.detail)
                          : 'Stream error';
                setLoadingText(null);
                setMessages((prev) => {
                    const newArr = [...prev];
                    if (newArr.length > 0) {
                        const last = newArr[newArr.length - 1];
                        if (last.role === 'bot') {
                            newArr[newArr.length - 1] = {
                                ...last,
                                content: last.content
                                    ? `${last.content}\n\n[Error]: ${detail}`
                                    : `[Error]: ${detail}`,
                            };
                        }
                    }
                    return newArr;
                });
                setStatus('error');
                break;
            }
            default:
                console.warn('[useChatStream] Unknown event type:', type, event);
        }
    }, []);

    const sendMessage = useCallback(
        async (
            userMessage: string,
            _resetContext: boolean,
            _authToken: string,
            _sessionId?: string | null,
            onInit?: (sessionId?: string) => void,
            onSessionTitle?: (title: string) => void
        ) => {
            onSessionTitleRef.current = onSessionTitle;
            setStatus('loading');
            setLoadingText('Initializing...');
            setMessages((prev) => [...prev, { role: 'user', content: userMessage }]);
            setOptions(null);
            currentBotMessage.current = '';

            // Placeholder for bot message
            setMessages((prev) => [...prev, { role: 'bot', content: '' }]);

            try {
            const streamSessionId =
                _sessionId && _sessionId.length > 0
                    ? _sessionId
                    : localStorage.getItem(CHAT_SESSION_ID_STORAGE_KEY);
            if (!streamSessionId) {
                throw new Error('No chat session id. Sign in again or start a session.');
            }

            onInit?.(streamSessionId);

            // Step 2: Stream
            const streamUrl = apiUrl(
              `/api/v1/chat/sessions/${encodeURIComponent(streamSessionId)}/messages/stream`
            );

            const streamBody = streamMessageBodySchema.parse({ content: userMessage });

            const response = await fetch(streamUrl, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'ngrok-skip-browser-warning': 'true',
                    Authorization: `Bearer ${_authToken}`,
                },
                body: JSON.stringify(streamBody),
            });

            if (!response.ok) {
                throw new Error(`Stream failed: ${response.statusText}`);
            }

            if (!response.body) {
                throw new Error('No response body for stream');
            }

            const reader = response.body.getReader();
            const decoder = new TextDecoder();
            let buffer = '';

            setStatus('streaming');

            while (true) {
                const { done, value } = await reader.read();
                if (done) break;

                buffer += decoder.decode(value, { stream: true });

                const lines = buffer.split('\n');
                buffer = lines.pop() || '';

                for (const line of lines) {
                    const trimmed = line.trim();
                    if (!trimmed || trimmed.startsWith(':')) continue;

                    let jsonStr = '';
                    if (trimmed.startsWith('data:')) {
                        jsonStr = trimmed.substring(5).trim();
                    } else if (trimmed.startsWith('{')) {
                        jsonStr = trimmed;
                    } else {
                        continue;
                    }

                    if (!jsonStr) continue;

                    try {
                        const parsed: unknown = JSON.parse(jsonStr);
                        const payload: Record<string, unknown> =
                            parsed &&
                            typeof parsed === 'object' &&
                            parsed !== null &&
                            'data' in parsed &&
                            (parsed as { data: unknown }).data !== undefined &&
                            typeof (parsed as { data: unknown }).data === 'object' &&
                            (parsed as { data: unknown }).data !== null
                                ? ((parsed as { data: Record<string, unknown> }).data)
                                : ((parsed as Record<string, unknown>));
                        handleEvent(payload);
                    } catch (e) {
                        console.warn('Failed to parse stream line:', line, e);
                    }
                }
            }

            const tail = buffer.trim();
            if (tail) {
                try {
                    const jsonStr = tail.startsWith('data:') ? tail.slice(5).trim() : tail;
                    const parsed: unknown = JSON.parse(jsonStr);
                    const payload: Record<string, unknown> =
                        parsed &&
                        typeof parsed === 'object' &&
                        parsed !== null &&
                        'data' in parsed &&
                        (parsed as { data: unknown }).data !== undefined &&
                        typeof (parsed as { data: unknown }).data === 'object' &&
                        (parsed as { data: unknown }).data !== null
                            ? (parsed as { data: Record<string, unknown> }).data
                            : (parsed as Record<string, unknown>);
                    handleEvent(payload);
                } catch (e) {
                    console.warn('Failed to parse trailing stream buffer:', tail, e);
                }
            }

            console.log('[useChatStream] Stream finished');
            setStatus('complete');
            setLoadingText(null);

            } catch (error) {
                console.error('Stream error:', error);
                setStatus('error');
                setMessages((prev) => {
                    const newArr = [...prev];
                    if (newArr.length > 0) {
                        const last = newArr[newArr.length - 1];
                        if (last.role === 'bot') {
                            last.content += `\n\n[System Error]: ${error instanceof Error ? error.message : 'Unknown error'}`;
                        }
                    }
                    return newArr;
                });
            } finally {
                onSessionTitleRef.current = undefined;
            }
        },
        [handleEvent]
    );

    return { messages, status, loadingText, sendMessage, resetChat, options, setMessages, setStatus };
}
