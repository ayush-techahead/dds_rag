import React, { useState, useEffect, useCallback, useRef } from 'react';
import { createPortal } from 'react-dom';
import { Loader2, MessageSquare, RefreshCw, Trash2, User } from 'lucide-react';
import { deleteChatSession } from '../api/deleteChatSession';
import { apiUrl } from '../lib/apiBase';
import './Sidebar.css';

/** Row from GET /api/v1/chat/sessions */
export interface Session {
  id: string;
  user_id: string;
  title: string | null;
  created_at: string;
  updated_at: string;
}

interface SidebarProps {
  onSelectSession: (session: Session) => void;
  onNewChat: () => void;
  onSessionDeleted?: (sessionId: string) => void;
  activeSessionId: string | null;
  refreshTrigger: number;
  authToken: string;
  user: { id?: string; email: string; full_name?: string | null } | null;
}

export const Sidebar: React.FC<SidebarProps> = ({
  onSelectSession,
  onNewChat,
  onSessionDeleted,
  activeSessionId,
  refreshTrigger,
  authToken,
  user,
}) => {
  const [sessions, setSessions] = useState<Session[]>([]);
  const [loading, setLoading] = useState(false);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [pendingDelete, setPendingDelete] = useState<Session | null>(null);
  const deleteInFlight = useRef(false);

  const fetchSessions = useCallback(async () => {
    if (!authToken) return;
    setLoading(true);
    try {
      const response = await fetch(apiUrl('/api/v1/chat/sessions'), {
        headers: {
          Authorization: `Bearer ${authToken}`,
        },
      });
      if (response.ok) {
        const data = (await response.json()) as Session[];
        setSessions(Array.isArray(data) ? data : []);
      } else {
        console.error('Failed to fetch sessions', response.status);
        setSessions([]);
      }
    } catch (e) {
      console.error('Error fetching sessions:', e);
      setSessions([]);
    } finally {
      setLoading(false);
    }
  }, [authToken]);

  useEffect(() => {
    if (authToken) {
      void fetchSessions();
    }
  }, [authToken, refreshTrigger, fetchSessions]);

  useEffect(() => {
    if (!pendingDelete) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setPendingDelete(null);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [pendingDelete]);

  const openDeleteConfirm = useCallback((session: Session, event: React.MouseEvent) => {
    event.stopPropagation();
    if (deleteInFlight.current || deletingId) return;
    setPendingDelete(session);
  }, [deletingId]);

  const confirmDeleteSession = useCallback(async () => {
    const session = pendingDelete;
    if (!session || deleteInFlight.current) return;
    setPendingDelete(null);
    deleteInFlight.current = true;
    setDeletingId(session.id);
    try {
      await deleteChatSession(authToken, session.id);
      setSessions((prev) => prev.filter((s) => s.id !== session.id));
      onSessionDeleted?.(session.id);
    } catch (e) {
      console.error('Failed to delete session:', e);
    } finally {
      deleteInFlight.current = false;
      setDeletingId(null);
    }
  }, [pendingDelete, authToken, onSessionDeleted]);

  const cancelDeleteConfirm = useCallback(() => {
    if (deleteInFlight.current) return;
    setPendingDelete(null);
  }, []);

  const displayName = user?.full_name?.trim() || user?.email || 'User';

  return (
    <div className="sidebar">
      <div className="sidebar-header">
        <div className="user-profile">
          <div className="user-avatar">
            <User size={20} />
          </div>
          <div className="user-details">
            <div className="user-name">{displayName}</div>
            <div className="user-email">{user?.email}</div>
          </div>
          <button
            onClick={() => void fetchSessions()}
            disabled={loading}
            className="refresh-btn-mini"
            title="Refresh Sessions"
            type="button"
          >
            <RefreshCw size={14} className={loading ? 'animate-spin' : ''} />
          </button>
        </div>
      </div>

      <div className="new-chat-container">
        <button
          type="button"
          className={`session-item new-chat-btn ${activeSessionId === null ? 'active' : ''}`}
          onClick={onNewChat}
        >
          <MessageSquare size={16} className="session-icon" />
          <div className="session-info">
            <span className="session-title">New Chat</span>
          </div>
        </button>
      </div>

      <div className="sessions-list">
        {sessions.length === 0 && !loading && (
          <div className="empty-state">No sessions found</div>
        )}
        {sessions.map((session) => (
          <div
            key={session.id}
            className={`session-row ${activeSessionId === session.id ? 'active' : ''}`}
          >
            <button
              type="button"
              className="session-item session-item-select"
              onClick={() => onSelectSession(session)}
            >
              <MessageSquare size={16} className="session-icon" />
              <div className="session-info">
                <span className="session-title">{session.title || 'New Session'}</span>
                <span className="session-date">
                  {new Date(session.updated_at).toLocaleString(undefined, {
                    month: 'short',
                    day: 'numeric',
                    hour: '2-digit',
                    minute: '2-digit',
                  })}
                </span>
              </div>
            </button>
            <button
              type="button"
              className="session-delete-btn"
              title="Delete session"
              disabled={deletingId === session.id}
              aria-label="Delete session"
              onClick={(e) => openDeleteConfirm(session, e)}
            >
              {deletingId === session.id ? (
                <Loader2 size={16} className="animate-spin" />
              ) : (
                <Trash2 size={16} />
              )}
            </button>
          </div>
        ))}
      </div>

      {typeof document !== 'undefined' &&
        pendingDelete &&
        createPortal(
          <div
            className="delete-confirm-backdrop"
            role="presentation"
            onClick={cancelDeleteConfirm}
          >
            <div
              className="delete-confirm-dialog"
              role="dialog"
              aria-modal="true"
              aria-labelledby="delete-confirm-title"
              onClick={(e) => e.stopPropagation()}
            >
              <h2 id="delete-confirm-title" className="delete-confirm-title">
                Delete this chat session?
              </h2>
              <p className="delete-confirm-body">
                This removes the session from your list. You will not be able to open it again.
              </p>
              <div className="delete-confirm-actions">
                <button type="button" className="delete-confirm-btn cancel" onClick={cancelDeleteConfirm}>
                  Cancel
                </button>
                <button
                  type="button"
                  className="delete-confirm-btn danger"
                  onClick={() => void confirmDeleteSession()}
                >
                  Delete
                </button>
              </div>
            </div>
          </div>,
          document.body
        )}
    </div>
  );
};
