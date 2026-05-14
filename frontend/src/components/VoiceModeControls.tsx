import { useEffect, useState } from 'react';
import { Loader2, Mic, MicOff, PhoneOff, Volume2 } from 'lucide-react';
import type { VoiceConnectionState, VoiceUiPhase } from '../hooks/useVoiceRealtime';
import './VoiceModeControls.css';

export type VoiceTranscriptMessage = {
  role: 'user' | 'bot';
  content: string;
  isStreaming?: boolean;
};

export type VoiceModeControlsProps = {
  connectionState: VoiceConnectionState;
  phase: VoiceUiPhase;
  captionUser: string;
  captionAssistant: string;
  voiceInputReadyNonce: number;
  errorMessage: string | null;
  /** Shown while the mic session is still live (e.g. voice/commit or tool bridge failed) */
  commitErrorMessage?: string | null;
  /** Assistant TTS in progress; the UI should discourage overlapping turns. */
  assistantPlaybackBlockingMic: boolean;
  disabled: boolean;
  layout?: 'composer' | 'full';
  transcriptMessages?: VoiceTranscriptMessage[];
  onStart: () => void;
  onStop: () => void;
};

export function VoiceModeControls({
  connectionState,
  phase,
  captionUser,
  captionAssistant,
  voiceInputReadyNonce,
  errorMessage,
  commitErrorMessage,
  assistantPlaybackBlockingMic,
  disabled,
  layout = 'composer',
  transcriptMessages = [],
  onStart,
  onStop,
}: VoiceModeControlsProps) {
  const live = connectionState === 'live';
  const busy = connectionState === 'connecting';
  const failed = connectionState === 'error';
  const active = live || busy;
  const micPausedForPlayback = live && assistantPlaybackBlockingMic;
  const [elapsedSeconds, setElapsedSeconds] = useState(0);

  useEffect(() => {
    if (!active) {
      return;
    }
    const startedAt = Date.now();
    const resetTimer = window.setTimeout(() => {
      setElapsedSeconds(0);
    }, 0);
    const timer = window.setInterval(() => {
      setElapsedSeconds(Math.max(0, Math.floor((Date.now() - startedAt) / 1000)));
    }, 1000);
    return () => {
      window.clearTimeout(resetTimer);
      window.clearInterval(timer);
    };
  }, [active]);

  const elapsedLabel = (() => {
    const totalSeconds = active ? elapsedSeconds : 0;
    const minutes = Math.floor(totalSeconds / 60);
    const seconds = totalSeconds % 60;
    return `${minutes}:${seconds.toString().padStart(2, '0')}`;
  })();

  const statusLabel = busy
    ? 'Connecting'
    : failed
      ? 'Voice unavailable'
      : phase === 'listening'
        ? 'Listening'
        : phase === 'thinking'
          ? 'Thinking'
          : phase === 'tool_lookup'
            ? 'Looking up docs'
            : phase === 'speaking'
              ? 'Speaking'
              : live
                ? micPausedForPlayback
                  ? 'Please wait'
                  : 'Ready'
                : 'Voice chat';

  const statusDetail = busy
    ? 'Preparing a secure realtime voice session.'
    : phase === 'listening'
      ? 'Speak naturally. Your words will appear in the chat.'
      : phase === 'thinking'
        ? 'The assistant is preparing a response.'
        : phase === 'tool_lookup'
          ? 'Preparing the most relevant answer.'
          : phase === 'speaking' || micPausedForPlayback
            ? 'Assistant is speaking. Ask your follow-up after the ready chime.'
            : live
              ? 'Ask your next question when ready.'
              : 'Start a voice conversation.';

  const liveInputCaption =
    captionUser.trim() ||
    (phase === 'listening' ? 'Listening…' : 'Your speech will appear here as it is transcribed.');
  const currentLiveInput = captionUser.trim();
  const visibleTranscript = transcriptMessages.filter((m) => {
    const content = m.content.trim();
    if (!content) return false;
    return !(layout === 'full' && m.role === 'user' && m.isStreaming && content === currentLiveInput);
  });

  const handleMicClick = () => {
    if (!live && !busy) {
      onStart();
    }
  };

  return (
    <div
      className={`voice-controls voice-controls-${layout} ${active ? 'voice-controls-active' : ''}`}
      aria-live="polite"
    >
      {active ? (
        <div className="voice-session-panel">
          <div className="voice-session-header">
            <div className="voice-status-copy">
              <div className="voice-status-line">
                <span className={`voice-status-dot voice-status-dot-${phase}`} aria-hidden />
                <span>{statusLabel}</span>
              </div>
              <p>{statusDetail}</p>
            </div>
            <div className="voice-session-timer" aria-label={`Voice session elapsed time ${elapsedLabel}`}>
              {elapsedLabel}
            </div>
          </div>

          <div className="voice-live-caption">
            <span className="voice-live-caption-label">Live input</span>
            <p className={captionUser.trim() ? undefined : 'voice-live-caption-empty'}>
              {busy ? 'Connecting to voice…' : liveInputCaption}
            </p>
          </div>

          {layout === 'full' && (
            <div className="voice-transcript-history" aria-label="Voice conversation transcript">
              {visibleTranscript.length > 0 ? (
                visibleTranscript.map((msg, index) => (
                  <div
                    key={`${msg.role}-${index}-${msg.content.slice(0, 16)}`}
                    className={`voice-transcript-row voice-transcript-row-${msg.role}`}
                  >
                    <span className="voice-transcript-speaker">
                      {msg.role === 'user' ? 'You' : 'Assistant'}
                    </span>
                    <p>
                      {msg.content}
                      {msg.isStreaming && <span className="voice-transcript-cursor" />}
                    </p>
                  </div>
                ))
              ) : (
                <p className="voice-transcript-empty">
                  Conversation transcript will appear here during the voice session.
                </p>
              )}
            </div>
          )}

          {layout !== 'full' && captionAssistant.trim() && (
            <div className="voice-live-caption">
              <span className="voice-live-caption-label">Assistant</span>
              <p>{captionAssistant}</p>
            </div>
          )}

          {voiceInputReadyNonce > 0 && !micPausedForPlayback && (
            <div key={voiceInputReadyNonce} className="voice-input-ready-hint">
              Ready for your follow-up.
            </div>
          )}

          {(errorMessage || commitErrorMessage) && (
            <div className="voice-errors-inline voice-errors-panel">
              {errorMessage && (
                <div className="voice-error-text" role="alert">
                  {errorMessage}
                </div>
              )}
              {commitErrorMessage && (
                <div className="voice-error-text voice-commit-warning" role="alert">
                  {commitErrorMessage}
                </div>
              )}
            </div>
          )}

          <div className="voice-session-actions">
            <button
              type="button"
              className={`voice-mic-btn ${live ? 'live' : ''} ${micPausedForPlayback ? 'playback-paused' : ''}`}
              onClick={handleMicClick}
              disabled={disabled || busy || live}
              title={live ? statusDetail : 'Start voice session'}
              aria-label={live ? statusLabel : 'Start voice session'}
              aria-busy={busy || micPausedForPlayback || undefined}
            >
              {busy ? (
                <Loader2 className="voice-mic-icon spin" size={22} />
              ) : micPausedForPlayback || phase === 'speaking' ? (
                <Volume2 size={22} className="voice-mic-icon" />
              ) : live ? (
                <Mic size={22} className="voice-mic-icon" />
              ) : (
                <MicOff size={22} className="voice-mic-icon" />
              )}
            </button>
            <button
              type="button"
              className="voice-end-call-btn"
              onClick={onStop}
              title="End voice session"
              aria-label="End voice session"
            >
              <PhoneOff size={18} aria-hidden />
              <span>End</span>
            </button>
          </div>
        </div>
      ) : (
        <div className="voice-controls-row voice-controls-row-compact">
          <button
            type="button"
            className={`voice-mic-btn ${live ? 'live' : ''} ${failed ? 'error' : ''} ${micPausedForPlayback ? 'playback-paused' : ''}`}
            onClick={handleMicClick}
            disabled={disabled || busy}
            title={
              live
                ? micPausedForPlayback
                  ? 'Assistant is speaking — ask your follow-up after the ready chime'
                  : 'End voice session'
                : 'Start voice session'
            }
            aria-pressed={live}
            aria-busy={micPausedForPlayback || undefined}
            aria-disabled={micPausedForPlayback || undefined}
          >
            {busy ? (
              <Loader2 className="voice-mic-icon spin" size={22} />
            ) : live ? (
              <Mic size={22} className="voice-mic-icon" />
            ) : (
              <MicOff size={22} className="voice-mic-icon" />
            )}
          </button>

          {errorMessage || (commitErrorMessage && live) ? (
            <div className="voice-errors-inline">
              {errorMessage && (
                <div className="voice-error-text" role="alert">
                  {errorMessage}
                </div>
              )}
              {commitErrorMessage && live && (
                <div className="voice-error-text voice-commit-warning" role="alert">
                  {commitErrorMessage}
                </div>
              )}
            </div>
          ) : null}
        </div>
      )}
    </div>
  );
}
