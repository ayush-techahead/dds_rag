import { Mic, MicOff, Loader2 } from 'lucide-react';
import type { VoiceConnectionState } from '../hooks/useVoiceRealtime';
import './VoiceModeControls.css';

export type VoiceModeControlsProps = {
  connectionState: VoiceConnectionState;
  errorMessage: string | null;
  /** Shown while the mic session is still live (e.g. voice/commit or tool bridge failed) */
  commitErrorMessage?: string | null;
  /** Assistant TTS in progress — mic capture is paused to avoid overlap */
  assistantPlaybackBlockingMic: boolean;
  disabled: boolean;
  onStart: () => void;
  onStop: () => void;
};

export function VoiceModeControls({
  connectionState,
  errorMessage,
  commitErrorMessage,
  assistantPlaybackBlockingMic,
  disabled,
  onStart,
  onStop,
}: VoiceModeControlsProps) {
  const live = connectionState === 'live';
  const busy = connectionState === 'connecting';
  const failed = connectionState === 'error';
  const micPausedForPlayback = live && assistantPlaybackBlockingMic;
  const handleMicClick = () => {
    if (!live) {
      onStart();
      return;
    }
    if (micPausedForPlayback) {
      return;
    }
    onStop();
  };

  return (
    <div className="voice-controls" aria-live="polite">
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

        {(errorMessage && failed) || (commitErrorMessage && live) ? (
          <div className="voice-errors-inline">
            {errorMessage && failed && (
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
    </div>
  );
}
