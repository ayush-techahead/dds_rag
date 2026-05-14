import { useRef, useEffect, useState, useCallback } from 'react';
import { createChatSession, CHAT_SESSION_ID_STORAGE_KEY } from './api/createChatSession';
import { fetchChatSessionDetail } from './api/fetchChatSessionDetail';
import { fetchCurrentUser } from './api/users';
import { getApiBaseUrl } from './lib/apiBase';
import { useChatStream, type Message } from './hooks/useChatStream';
import { useVoiceRealtime } from './hooks/useVoiceRealtime';
import {
  cleanupVoiceMessagesAfterStop,
  mergeVoiceCommitMessages,
  reduceVoiceThreadEvent,
} from './voiceChatMerge';
import { ChatInput } from './components/ChatInput';
import { ChatMessage } from './components/ChatMessage';
import { Login } from './components/Login';
import { Sidebar, type Session } from './components/Sidebar';
import { VoiceModeControls } from './components/VoiceModeControls';
import { MessageSquare, Menu, AlertTriangle, LogOut } from 'lucide-react';
import './App.css';

function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms));
}

/** Voice commit returns before async server title generation; poll session detail (see FRONTEND_VOICE_INTEGRATION §7.4). */
async function pollSessionTitleAfterVoiceCommit(
  token: string,
  sessionId: string,
  applyTitle: (title: string) => void
): Promise<void> {
  for (let i = 0; i < 5; i++) {
    await sleep(500);
    try {
      const detail = await fetchChatSessionDetail(token, sessionId);
      const t = typeof detail.title === 'string' ? detail.title.trim() : '';
      if (t) {
        applyTitle(t);
        return;
      }
    } catch {
      /* ignore */
    }
  }
}

async function getVoicePreflightError(): Promise<string | null> {
  if (typeof window !== 'undefined' && !window.isSecureContext) {
    return 'Voice chat needs a secure browser context. Use HTTPS or localhost, then try again.';
  }
  if (
    typeof navigator === 'undefined' ||
    !navigator.mediaDevices ||
    typeof navigator.mediaDevices.getUserMedia !== 'function'
  ) {
    return 'This browser does not support microphone capture for voice chat.';
  }

  try {
    const permission = await navigator.permissions?.query({
      name: 'microphone' as PermissionName,
    });
    if (permission?.state === 'denied') {
      return 'Microphone access is blocked. Allow mic access in your browser settings, then start voice again.';
    }
  } catch {
    /* Browser does not expose microphone permission status before prompting. */
  }

  return null;
}

function App() {
  const { messages, status, loadingText, sendMessage, options, setMessages, setStatus } = useChatStream();
  const {
    connectionState: voiceConnectionState,
    phase: voicePhase,
    captionUser: voiceCaptionUser,
    captionAssistant: voiceCaptionAssistant,
    voiceInputReadyNonce,
    errorMessage: voiceError,
    commitErrorMessage: voiceCommitError,
    connect: voiceConnect,
    disconnect: voiceDisconnect,
    assistantPlaybackBlockingMic,
  } = useVoiceRealtime();

  const [authToken, setAuthToken] = useState<string | null>(localStorage.getItem('authToken'));
  const [user, setUser] = useState<{
    id?: string;
    email: string;
    full_name?: string | null;
  } | null>(() => {
    const savedUser = localStorage.getItem('user');
    if (!savedUser) return null;
    try {
      return JSON.parse(savedUser) as { id?: string; email: string; full_name?: string | null };
    } catch {
      return null;
    }
  });

  const [activeSessionId, setActiveSessionId] = useState<string | null>(null);
  const [activeSessionTitle, setActiveSessionTitle] = useState<string | null>(null);
  const [showSidebar, setShowSidebar] = useState(false);
  const [refreshSidebar, setRefreshSidebar] = useState(0);
  const [voiceStartError, setVoiceStartError] = useState<string | null>(null);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  /** Avoid polling GET /sessions for async title on every voice turn (§7.4). */
  const voiceTitlePollSessionRef = useRef<string | null>(null);
  const activeSessionIdRef = useRef<string | null>(activeSessionId);
  const combinedVoiceError = voiceStartError ?? voiceError;
  const voiceActive = voiceConnectionState === 'live' || voiceConnectionState === 'connecting';

  const applyServerSessionTitle = useCallback((title: string) => {
    setActiveSessionTitle(title);
    setRefreshSidebar((prev) => prev + 1);
  }, []);

  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  };

  useEffect(() => {
    scrollToBottom();
  }, [messages, status, voiceConnectionState]);

  useEffect(() => {
    activeSessionIdRef.current = activeSessionId;
  }, [activeSessionId]);

  useEffect(() => {
    return () => {
      voiceDisconnect('app_unmount');
    };
  }, [voiceDisconnect]);

  const handleVoiceStart = useCallback(async () => {
    if (!authToken) return;
    setVoiceStartError(null);
    const preflightError = await getVoicePreflightError();
    if (preflightError) {
      setVoiceStartError(preflightError);
      return;
    }

    let sessionId = activeSessionId;
    if (!sessionId) {
      try {
        const session = await createChatSession(authToken);
        sessionId = session.id;
        setActiveSessionId(session.id);
        activeSessionIdRef.current = session.id;
        setActiveSessionTitle(session.title ?? 'New Session');
        setRefreshSidebar((prev) => prev + 1);
      } catch (e) {
        console.error('[App] Voice: failed to create session', e);
        setVoiceStartError('Could not create a chat session for voice. Please try again.');
        return;
      }
    }
    try {
      setMessages((p) => p.filter((m) => !m.optimisticVoice));
      await voiceConnect(authToken, sessionId, {
        onVoiceThreadEvent: (ev) => {
          setMessages((prev) => reduceVoiceThreadEvent(prev, ev));
        },
        onTurnCommitted: ({ messages: committed, sessionTitle, chatSessionId, clientTurnId }) => {
          if (activeSessionIdRef.current !== chatSessionId) {
            return;
          }
          setMessages((prev) => mergeVoiceCommitMessages(prev, committed, clientTurnId));
          if (sessionTitle && sessionTitle.trim().length > 0) {
            applyServerSessionTitle(sessionTitle.trim());
            voiceTitlePollSessionRef.current = chatSessionId;
          } else if (voiceTitlePollSessionRef.current !== chatSessionId) {
            voiceTitlePollSessionRef.current = chatSessionId;
            void pollSessionTitleAfterVoiceCommit(authToken, chatSessionId, applyServerSessionTitle);
          }
          setRefreshSidebar((prev) => prev + 1);
        },
      });
    } catch (e) {
      console.error('[App] Voice connect failed:', e);
      setVoiceStartError(e instanceof Error ? e.message : 'Voice connection failed. Please try again.');
    }
  }, [
    activeSessionId,
    authToken,
    applyServerSessionTitle,
    setMessages,
    voiceConnect,
  ]);

  const handleVoiceStop = useCallback(() => {
    setVoiceStartError(null);
    voiceDisconnect('user_stop');
    setMessages((prev) => cleanupVoiceMessagesAfterStop(prev));
  }, [setMessages, voiceDisconnect]);

  const handleSessionDeleted = useCallback(
    (sessionId: string) => {
      if (activeSessionId === sessionId) {
        voiceDisconnect('session_deleted');
        setVoiceStartError(null);
        setActiveSessionId(null);
        setActiveSessionTitle('New Session');
        setMessages([]);
        setStatus('idle');
        setShowSidebar(false);
        localStorage.removeItem(CHAT_SESSION_ID_STORAGE_KEY);
      }
      setRefreshSidebar((prev) => prev + 1);
    },
    [activeSessionId, setMessages, setStatus, voiceDisconnect]
  );

  if (!authToken) {
    return (
      <Login
        onLoginSuccess={async (token, _accountEmail, userData) => {
          setAuthToken(token);
          setUser(userData);
          localStorage.setItem('authToken', token);
          localStorage.setItem('user', JSON.stringify(userData));

          try {
            const me = await fetchCurrentUser(token);
            const merged = {
              id: me.id,
              email: me.email,
              full_name: me.full_name,
            };
            setUser(merged);
            localStorage.setItem('user', JSON.stringify(merged));
          } catch (e) {
            console.warn('[App] GET /users/me failed; using login identity only:', e);
          }

          try {
            const session = await createChatSession(token);
            setActiveSessionId(session.id);
            setActiveSessionTitle(session.title ?? 'New Session');
            setRefreshSidebar((prev) => prev + 1);
          } catch (e) {
            console.error('[App] Failed to create chat session after login:', e);
          }
        }}
      />
    );
  }

  const handleSelectSession = async (session: Session) => {
    if (session.id === activeSessionId) return;

    voiceDisconnect('session_selected');
    setVoiceStartError(null);
    setActiveSessionId(session.id);
    setActiveSessionTitle(session.title ?? 'Session');
    localStorage.setItem(CHAT_SESSION_ID_STORAGE_KEY, session.id);
    setStatus('loading');
    setMessages([]);
    setShowSidebar(false);

    try {
      const detail = await fetchChatSessionDetail(authToken, session.id);
      if (typeof detail.title === 'string' && detail.title.trim().length > 0) {
        setActiveSessionTitle(detail.title.trim());
      }
      const history = detail.messages ?? [];
      const mappedMessages: Message[] = history.map((h) => {
        const row = h as {
          id?: string;
          role: string;
          content: string;
          source?: string;
        };
        const src =
          row.source === 'voice' || row.source === 'text'
            ? (row.source as 'voice' | 'text')
            : undefined;
        return {
          role: row.role === 'assistant' ? 'bot' : 'user',
          content: row.content,
          options: [],
          ...(typeof row.id === 'string' && row.id.length > 0 ? { serverMessageId: row.id } : {}),
          ...(src ? { source: src } : {}),
        };
      });
      setMessages(mappedMessages);
      setStatus('idle');
    } catch (e) {
      console.error('Error loading session:', e);
      setStatus('error');
    }
  };

  const handleNewChat = () => {
    voiceDisconnect('new_chat');
    voiceTitlePollSessionRef.current = null;
    setActiveSessionId(null);
    setActiveSessionTitle('New Session');
    setMessages([]);
    setStatus('idle');
    setShowSidebar(false);
    localStorage.removeItem(CHAT_SESSION_ID_STORAGE_KEY);
  };

  const handleLogout = () => {
    voiceDisconnect('logout');
    setAuthToken(null);
    setUser(null);
    setActiveSessionId(null);
    localStorage.removeItem('authToken');
    localStorage.removeItem('user');
    localStorage.removeItem('type');
    localStorage.removeItem(CHAT_SESSION_ID_STORAGE_KEY);
  };

  return (
    <div className="app-container flex-row">
      <div className={`sidebar-wrapper ${showSidebar ? 'open' : ''}`}>
        <Sidebar
          onSelectSession={handleSelectSession}
          onNewChat={handleNewChat}
          onSessionDeleted={handleSessionDeleted}
          activeSessionId={activeSessionId}
          refreshTrigger={refreshSidebar}
          authToken={authToken}
          user={user}
        />
      </div>

      {showSidebar && <div className="sidebar-overlay" onClick={() => setShowSidebar(false)} />}

      <div className="main-content">
        <header className="header">
          <div className="header-left">
            <button type="button" className="mobile-menu-btn" onClick={() => setShowSidebar(!showSidebar)}>
              <Menu size={24} />
            </button>
            <div className="flex items-center">
              <span className="text-xl font-bold">DDS Demo Bot</span>
            </div>
          </div>

          <div className="header-center">
            {activeSessionTitle && <div className="session-title-text">{activeSessionTitle}</div>}
          </div>

          <div className="header-right">
            <button
              type="button"
              onClick={handleLogout}
              className="logout-btn-mini"
              title="Logout"
              style={{
                background: 'none',
                border: 'none',
                cursor: 'pointer',
                color: 'var(--color-text-muted)',
                display: 'flex',
                alignItems: 'center',
                padding: '0.5rem',
              }}
            >
              <LogOut size={20} />
            </button>
          </div>
        </header>

        <main className={`chat-area ${voiceActive ? 'chat-area-voice-mode' : ''}`}>
          {voiceActive ? (
            <section className="voice-mode-screen" aria-label="Active voice chat">
              <div className="voice-mode-intro">
                <span className="voice-mode-kicker">Voice chat</span>
                <h2>{activeSessionTitle ?? 'DDS Demo Bot'}</h2>
                <p>Your chat history is hidden while voice is live. End the session to return to the transcript.</p>
              </div>
              <VoiceModeControls
                connectionState={voiceConnectionState}
                phase={voicePhase}
                captionUser={voiceCaptionUser}
                captionAssistant={voiceCaptionAssistant}
                voiceInputReadyNonce={voiceInputReadyNonce}
                errorMessage={combinedVoiceError}
                commitErrorMessage={voiceCommitError}
                assistantPlaybackBlockingMic={assistantPlaybackBlockingMic}
                disabled={status === 'loading' || status === 'streaming'}
                layout="full"
                transcriptMessages={messages
                  .filter((m) => m.source === 'voice' || m.optimisticVoice)
                  .map((m) => ({
                    role: m.role,
                    content: m.content,
                    isStreaming: Boolean(m.voiceUserStreaming || m.voiceAssistantStreaming),
                  }))}
                onStart={() => void handleVoiceStart()}
                onStop={handleVoiceStop}
              />
            </section>
          ) : (
            <div className="chat-content-width">
              {messages.length === 0 && (
                <div className="welcome-container">
                  <MessageSquare size={48} className="welcome-icon" />
                  <div className="welcome-text">
                    <h2>Welcome to DDS Demo Bot</h2>
                    <p>Start a conversation to know more about DDS and related services.</p>
                  </div>
                </div>
              )}

              {messages.map((msg, index) => (
                <ChatMessage
                  key={msg.serverMessageId ?? `local-${index}`}
                  role={msg.role}
                  content={msg.content}
                  source={msg.source}
                  isStreaming={
                    (index === messages.length - 1 && (status === 'streaming' || status === 'loading'))
                  }
                  loadingText={
                    index === messages.length - 1 && (status === 'loading' || status === 'streaming')
                      ? loadingText
                      : null
                  }
                  options={index === messages.length - 1 ? options || undefined : undefined}
                  onOptionClick={(opt) =>
                    void sendMessage(
                      opt,
                      activeSessionId === null,
                      authToken,
                      activeSessionId,
                      undefined,
                      applyServerSessionTitle
                    )
                  }
                />
              ))}

              {status === 'error' && (
                <div className="flex justify-center p-4">
                  <div
                    style={{
                      color: '#ef4444',
                      display: 'flex',
                      alignItems: 'center',
                      gap: '0.5rem',
                      backgroundColor: 'rgba(239,68,68,0.1)',
                      padding: '0.5rem 1rem',
                      borderRadius: '0.5rem',
                    }}
                  >
                    <AlertTriangle size={16} />
                    <span>Connection failed. Please check backend is running on {getApiBaseUrl()}.</span>
                  </div>
                </div>
              )}

              <div ref={messagesEndRef} />
            </div>
          )}
        </main>

        {!voiceActive && (
          <footer className="input-area">
            <div className="input-area-composer">
              <div className="input-area-voice-slot">
                <VoiceModeControls
                  connectionState={voiceConnectionState}
                  phase={voicePhase}
                  captionUser={voiceCaptionUser}
                  captionAssistant={voiceCaptionAssistant}
                  voiceInputReadyNonce={voiceInputReadyNonce}
                  errorMessage={combinedVoiceError}
                  commitErrorMessage={voiceCommitError}
                  assistantPlaybackBlockingMic={assistantPlaybackBlockingMic}
                  disabled={status === 'loading' || status === 'streaming'}
                  onStart={() => void handleVoiceStart()}
                  onStop={handleVoiceStop}
                />
              </div>
              <div className="input-area-text-slot">
                <ChatInput
                  onSend={async (msg) => {
                    const isNewChat = activeSessionId === null;
                    let sessionId = activeSessionId;

                    if (isNewChat) {
                      try {
                        const session = await createChatSession(authToken);
                        sessionId = session.id;
                        setActiveSessionId(session.id);
                        setActiveSessionTitle(session.title ?? 'New Session');
                        setRefreshSidebar((prev) => prev + 1);
                      } catch (e) {
                        console.error('[App] Failed to create session for new chat:', e);
                        setStatus('error');
                        return;
                      }
                    }

                    await sendMessage(
                      msg,
                      isNewChat,
                      authToken,
                      sessionId,
                      () => {
                        if (isNewChat) {
                          setRefreshSidebar((prev) => prev + 1);
                        }
                      },
                      applyServerSessionTitle
                    );
                  }}
                  disabled={
                    status === 'loading' ||
                    status === 'streaming'
                  }
                />
              </div>
            </div>
            <div
              style={{
                textAlign: 'center',
                padding: '0.5rem',
                color: 'var(--color-text-muted)',
                fontSize: '0.75rem',
              }}
            >
              DDS Demo Bot can make mistakes. Consider checking important information.
            </div>
          </footer>
        )}
      </div>
    </div>
  );
}

export default App;
