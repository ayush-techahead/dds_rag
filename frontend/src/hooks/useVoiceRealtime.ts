import { useCallback, useRef, useState } from 'react';
import type { ChatMessageResponse } from '../api/commitVoiceTurn';
import {
  commitVoiceTurn,
  VoiceCommitSessionNotFoundError,
  VoiceCommitValidationError,
} from '../api/commitVoiceTurn';
import { lookupDocumentationRealtime } from '../api/lookupDocumentationRealtime';
import { mintRealtimeSession } from '../api/mintRealtimeSession';
import { openAiRealtimeWebRtcSdpUrl } from '../lib/openaiRealtimeBase';
import {
  createPlaybackTailWatcher,
  createRemotePlaybackGraphFromStream,
  PLAYBACK_TAIL_FALLBACK_DELAY_MS,
  RESPONSE_DONE_DEFERRED_TAIL_ARM_MS,
  type PlaybackTailWatcher,
  type RemotePlaybackGraph,
} from '../lib/voiceRemotePlaybackGate';

export type VoiceThreadEvent =
  | { type: 'speech_started' }
  | { type: 'user_transcript_delta'; itemId: string; text: string }
  | { type: 'user_transcript_final'; text: string }
  | { type: 'assistant_transcript_delta'; responseId: string; text: string }
  | { type: 'assistant_transcript_final'; responseId: string; text: string };

export type VoiceConnectOptions = {
  onTurnCommitted?: (detail: {
    messages: ChatMessageResponse[];
    sessionTitle: string | null;
    chatSessionId: string;
  }) => void;
  /** Drive chat bubbles while OpenAI streams transcripts */
  onVoiceThreadEvent?: (event: VoiceThreadEvent) => void;
};

export type VoiceConnectionState = 'off' | 'connecting' | 'live' | 'error';

export type VoiceUiPhase =
  | 'idle'
  | 'connecting'
  | 'listening'
  | 'thinking'
  | 'speaking'
  | 'tool_lookup';

type RealtimeEvent = {
  type?: string;
  session?: {
    turn_detection?: {
      type?: string;
      interrupt_response?: boolean;
    } | null;
    audio?: {
      input?: {
        turn_detection?: {
          type?: string;
          interrupt_response?: boolean;
        } | null;
      };
    };
  };
  response?: {
    id?: string;
    status?: string;
    output?: Array<{
      type?: string;
      name?: string;
      call_id?: string;
      arguments?: string;
    }>;
  };
  delta?: string;
  transcript?: string;
  response_id?: string;
};

type RealtimeTurnDetection = {
  type?: string;
  interrupt_response?: boolean;
};

function getRealtimeTurnDetection(evt: RealtimeEvent): RealtimeTurnDetection | null {
  return evt.session?.audio?.input?.turn_detection ?? evt.session?.turn_detection ?? null;
}

function sendJson(dc: RTCDataChannel, payload: unknown) {
  if (dc.readyState === 'open') {
    dc.send(JSON.stringify(payload));
  }
}

function playVoiceInputReadyChime() {
  try {
    const AC =
      typeof AudioContext !== 'undefined'
        ? AudioContext
        : (typeof window !== 'undefined'
            ? (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext
            : undefined);
    if (!AC) return;
    const ctx = new AC();
    const g = ctx.createGain();
    g.connect(ctx.destination);
    g.gain.value = 0.06;
    const playTone = (freq: number, start: number, dur: number) => {
      const o = ctx.createOscillator();
      o.type = 'sine';
      o.frequency.value = freq;
      o.connect(g);
      o.start(start);
      o.stop(start + dur);
    };
    const t0 = ctx.currentTime;
    playTone(784, t0, 0.07);
    playTone(988, t0 + 0.09, 0.1);
    window.setTimeout(() => {
      void ctx.close().catch(() => {});
    }, 450);
  } catch {
    /* ignore */
  }
}

export function useVoiceRealtime() {
  const [connectionState, setConnectionState] = useState<VoiceConnectionState>('off');
  const [phase, setPhase] = useState<VoiceUiPhase>('idle');
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [captionUser, setCaptionUser] = useState('');
  const [captionAssistant, setCaptionAssistant] = useState('');

  const pcRef = useRef<RTCPeerConnection | null>(null);
  const dcRef = useRef<RTCDataChannel | null>(null);
  const mediaStreamRef = useRef<MediaStream | null>(null);
  /** Local mic track — disabled while assistant audio streams so speaker bleed cannot trigger server VAD. */
  const micTrackRef = useRef<MediaStreamTrack | null>(null);
  const remoteAudioRef = useRef<HTMLAudioElement | null>(null);
  /** Web Audio graph for remote playback RMS (same path the user hears). */
  const playbackGraphRef = useRef<RemotePlaybackGraph | null>(null);
  const playbackTailWatcherRef = useRef<PlaybackTailWatcher | null>(null);
  const pendingPlaybackReleaseResponseIdRef = useRef<string | null>(null);
  const playbackTailArmedRef = useRef(false);
  /** True from first assistant transcript/audio delta until `releaseMicAfterAssistantPlayback` (guards speech_started UX). */
  const assistantOutputActiveRef = useRef(false);
  const releaseMicAfterAssistantPlaybackRef = useRef<(responseId: string | null) => void>(() => {});
  const authRef = useRef<{ token: string; sessionId: string } | null>(null);
  const onTurnCommittedRef = useRef<VoiceConnectOptions['onTurnCommitted'] | undefined>(undefined);
  const onVoiceThreadEventRef = useRef<VoiceConnectOptions['onVoiceThreadEvent'] | undefined>(undefined);
  const assistantTranscriptByResponseRef = useRef<Map<string, string>>(new Map());
  const userTranscriptByItemRef = useRef<Map<string, string>>(new Map());
  const lastUserTranscriptRef = useRef('');
  const toolQueueRef = useRef(Promise.resolve());
  const functionCallArgsBufferRef = useRef<Map<string, string>>(new Map());
  const handledFunctionCallIdsRef = useRef<Set<string>>(new Set());
  const currentTurnClientIdRef = useRef<string | null>(null);
  const [commitErrorMessage, setCommitErrorMessage] = useState<string | null>(null);
  /** Bumped when assistant audio finishes and the mic is opened again for the user. */
  const [voiceInputReadyNonce, setVoiceInputReadyNonce] = useState(0);
  /** True while assistant TTS is in progress — mic track is disabled and UI should reflect that. */
  const [assistantPlaybackBlockingMic, setAssistantPlaybackBlockingMic] = useState(false);

  const sawOutputAudioDeltaRef = useRef(false);
  const sawOutputAudioTranscriptDeltaRef = useRef(false);
  const deferredMicReleaseTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  /** `response_id`s for which we already ran post-assistant mic release + cue */
  const releasedMicForResponseIdsRef = useRef<Set<string>>(new Set());

  const clearAllMicReleaseScheduling = useCallback(() => {
    playbackTailWatcherRef.current?.cancel();
    if (deferredMicReleaseTimerRef.current !== null) {
      clearTimeout(deferredMicReleaseTimerRef.current);
      deferredMicReleaseTimerRef.current = null;
    }
    pendingPlaybackReleaseResponseIdRef.current = null;
    playbackTailArmedRef.current = false;
  }, []);

  const enqueueTool = useCallback((fn: () => Promise<void>) => {
    toolQueueRef.current = toolQueueRef.current.then(fn).catch((e) => {
      console.error('[voice] tool chain error', e);
      const msg = e instanceof Error ? e.message : String(e);
      setCommitErrorMessage(`Documentation lookup failed: ${msg}`);
    });
  }, []);

  const setLocalMicEnabled = useCallback((enabled: boolean) => {
    const t = micTrackRef.current;
    if (!t || t.readyState !== 'live') return;
    try {
      t.enabled = enabled;
    } catch {
      /* ignore */
    }
  }, []);

  const releaseMicAfterAssistantPlayback = useCallback(
    (responseId: string | null) => {
      if (!authRef.current) return;
      const rid = responseId && responseId.length > 0 ? responseId : 'unknown-response';
      if (releasedMicForResponseIdsRef.current.has(rid)) return;
      releasedMicForResponseIdsRef.current.add(rid);
      clearAllMicReleaseScheduling();
      setLocalMicEnabled(true);
      setAssistantPlaybackBlockingMic(false);
      assistantOutputActiveRef.current = false;
      setPhase('idle');
      setVoiceInputReadyNonce((n) => n + 1);
      playVoiceInputReadyChime();
      sawOutputAudioDeltaRef.current = false;
      sawOutputAudioTranscriptDeltaRef.current = false;
    },
    [clearAllMicReleaseScheduling, setLocalMicEnabled]
  );

  const scheduleDeferredMicRelease = useCallback(
    (responseId: string | null, delayMs: number) => {
      playbackTailWatcherRef.current?.cancel();
      playbackTailArmedRef.current = false;
      pendingPlaybackReleaseResponseIdRef.current = null;
      if (deferredMicReleaseTimerRef.current !== null) {
        clearTimeout(deferredMicReleaseTimerRef.current);
        deferredMicReleaseTimerRef.current = null;
      }
      deferredMicReleaseTimerRef.current = window.setTimeout(() => {
        deferredMicReleaseTimerRef.current = null;
        releaseMicAfterAssistantPlayback(responseId);
      }, delayMs);
    },
    [releaseMicAfterAssistantPlayback]
  );

  const armPlaybackTailWatch = useCallback(
    (responseId: string | null) => {
      playbackTailWatcherRef.current?.cancel();
      if (deferredMicReleaseTimerRef.current !== null) {
        clearTimeout(deferredMicReleaseTimerRef.current);
        deferredMicReleaseTimerRef.current = null;
      }
      const watcher = playbackTailWatcherRef.current;
      const graph = playbackGraphRef.current;
      if (watcher && graph && graph.audioContext.state !== 'closed') {
        pendingPlaybackReleaseResponseIdRef.current = responseId;
        playbackTailArmedRef.current = true;
        void graph.audioContext.resume().then(() => {
          if (!authRef.current || !playbackTailWatcherRef.current) {
            playbackTailArmedRef.current = false;
            pendingPlaybackReleaseResponseIdRef.current = null;
            return;
          }
          playbackTailWatcherRef.current.arm();
        }).catch(() => {
          playbackTailArmedRef.current = false;
          pendingPlaybackReleaseResponseIdRef.current = null;
          scheduleDeferredMicRelease(responseId, PLAYBACK_TAIL_FALLBACK_DELAY_MS);
        });
      } else {
        scheduleDeferredMicRelease(responseId, PLAYBACK_TAIL_FALLBACK_DELAY_MS);
      }
    },
    [scheduleDeferredMicRelease]
  );

  /** `response.done` can race ahead of `output_audio.done` / local audio — wait before arming RMS tail. */
  const schedulePlaybackTailArmAfterDelay = useCallback(
    (responseId: string | null, delayMs: number) => {
      playbackTailWatcherRef.current?.cancel();
      playbackTailArmedRef.current = false;
      pendingPlaybackReleaseResponseIdRef.current = null;
      if (deferredMicReleaseTimerRef.current !== null) {
        clearTimeout(deferredMicReleaseTimerRef.current);
        deferredMicReleaseTimerRef.current = null;
      }
      deferredMicReleaseTimerRef.current = window.setTimeout(() => {
        deferredMicReleaseTimerRef.current = null;
        if (!authRef.current) return;
        armPlaybackTailWatch(responseId);
      }, delayMs);
    },
    [armPlaybackTailWatch]
  );

  const disconnect = useCallback(() => {
    authRef.current = null;
    setPhase('idle');
    setConnectionState('off');
    setCaptionAssistant('');
    setCaptionUser('');
    setCommitErrorMessage(null);
    setAssistantPlaybackBlockingMic(false);
    clearAllMicReleaseScheduling();
    assistantOutputActiveRef.current = false;
    if (playbackGraphRef.current) {
      try {
        playbackGraphRef.current.dispose();
      } catch {
        /* ignore */
      }
      playbackGraphRef.current = null;
    }
    playbackTailWatcherRef.current = null;
    sawOutputAudioDeltaRef.current = false;
    sawOutputAudioTranscriptDeltaRef.current = false;
    releasedMicForResponseIdsRef.current.clear();
    setVoiceInputReadyNonce(0);
    functionCallArgsBufferRef.current.clear();
    handledFunctionCallIdsRef.current.clear();
    currentTurnClientIdRef.current = null;

    if (dcRef.current) {
      try {
        dcRef.current.close();
      } catch {
        /* ignore */
      }
      dcRef.current = null;
    }
    if (pcRef.current) {
      try {
        pcRef.current.close();
      } catch {
        /* ignore */
      }
      pcRef.current = null;
    }
    if (mediaStreamRef.current) {
      setLocalMicEnabled(true);
      micTrackRef.current = null;
      for (const t of mediaStreamRef.current.getTracks()) {
        t.stop();
      }
      mediaStreamRef.current = null;
    }
    if (remoteAudioRef.current) {
      try {
        remoteAudioRef.current.pause();
      } catch {
        /* ignore */
      }
      remoteAudioRef.current.srcObject = null;
      remoteAudioRef.current.remove();
      remoteAudioRef.current = null;
    }
    onTurnCommittedRef.current = undefined;
    onVoiceThreadEventRef.current = undefined;
  }, [clearAllMicReleaseScheduling, setLocalMicEnabled]);

  const handleRealtimeEvent = useCallback(
    (raw: string) => {
      let evt: RealtimeEvent;
      try {
        evt = JSON.parse(raw) as RealtimeEvent;
      } catch {
        return;
      }
      const typeRaw = evt.type ?? '';
      const type =
        typeRaw === 'response.audio_transcript.delta'
          ? 'response.output_audio_transcript.delta'
          : typeRaw === 'response.audio_transcript.done'
            ? 'response.output_audio_transcript.done'
            : typeRaw === 'response.audio.delta'
              ? 'response.output_audio.delta'
              : typeRaw === 'response.audio.done'
                ? 'response.output_audio.done'
                : typeRaw;

      if (type === 'session.updated') {
        const turnDetection = getRealtimeTurnDetection(evt);
        if (
          turnDetection?.type === 'server_vad' &&
          turnDetection.interrupt_response !== false
        ) {
          console.warn(
            '[voice] Realtime session did not accept interrupt_response=false; follow-up speech may interrupt assistant playback',
            turnDetection
          );
        }
        return;
      }

      if (type === 'conversation.item.input_audio_transcription.delta') {
        const itemId =
          typeof (evt as { item_id?: string }).item_id === 'string'
            ? (evt as { item_id: string }).item_id
            : 'default';
        const delta = typeof (evt as { delta?: string }).delta === 'string' ? (evt as { delta: string }).delta : '';
        if (!delta) return;
        const prev = userTranscriptByItemRef.current.get(itemId) ?? '';
        const next = prev + delta;
        userTranscriptByItemRef.current.set(itemId, next);
        setCaptionUser(next);
        onVoiceThreadEventRef.current?.({ type: 'user_transcript_delta', itemId, text: next });
        return;
      }

      if (type === 'conversation.item.input_audio_transcription.completed') {
        const t = typeof (evt as { transcript?: string }).transcript === 'string'
          ? (evt as { transcript: string }).transcript
          : '';
        lastUserTranscriptRef.current = t;
        setCaptionUser(t);
        onVoiceThreadEventRef.current?.({ type: 'user_transcript_final', text: t });
        return;
      }

      if (type === 'input_audio_buffer.speech_started') {
        if (assistantOutputActiveRef.current || playbackTailArmedRef.current) {
          return;
        }
        userTranscriptByItemRef.current.clear();
        setCaptionAssistant('');
        setCommitErrorMessage(null);
        if (!playbackTailArmedRef.current && !assistantOutputActiveRef.current) {
          setAssistantPlaybackBlockingMic(false);
        }
        try {
          currentTurnClientIdRef.current = crypto.randomUUID();
        } catch {
          currentTurnClientIdRef.current = `turn-${Date.now()}-${Math.random().toString(36).slice(2, 11)}`;
        }
        setPhase('listening');
        onVoiceThreadEventRef.current?.({ type: 'speech_started' });
        return;
      }
      if (type === 'input_audio_buffer.speech_stopped') {
        if (assistantOutputActiveRef.current || playbackTailArmedRef.current) {
          return;
        }
        setPhase('thinking');
        return;
      }

      if (type === 'response.output_audio.delta') {
        clearAllMicReleaseScheduling();
        sawOutputAudioDeltaRef.current = true;
        assistantOutputActiveRef.current = true;
        setAssistantPlaybackBlockingMic(true);
        setLocalMicEnabled(false);
        setPhase('speaking');
        return;
      }

      if (type === 'response.output_audio.done') {
        const rid =
          typeof evt.response_id === 'string' && evt.response_id.length > 0
            ? evt.response_id
            : typeof evt.response?.id === 'string' && evt.response.id.length > 0
              ? evt.response.id
              : null;
        armPlaybackTailWatch(rid);
        return;
      }

      if (type === 'response.output_audio_transcript.delta') {
        /** Stop speaker bleed from reaching OpenAI VAD while assistant TTS is streaming. */
        sawOutputAudioTranscriptDeltaRef.current = true;
        assistantOutputActiveRef.current = true;
        setAssistantPlaybackBlockingMic(true);
        setLocalMicEnabled(false);
        const rid =
          typeof evt.response_id === 'string'
            ? evt.response_id
            : typeof (evt as { response?: { id?: string } }).response?.id === 'string'
              ? (evt as { response: { id: string } }).response.id
              : '';
        const delta = typeof evt.delta === 'string' ? evt.delta : '';
        if (rid && delta) {
          const prev = assistantTranscriptByResponseRef.current.get(rid) ?? '';
          const next = prev + delta;
          assistantTranscriptByResponseRef.current.set(rid, next);
          setCaptionAssistant(next);
          onVoiceThreadEventRef.current?.({
            type: 'assistant_transcript_delta',
            responseId: rid,
            text: next,
          });
        }
        setPhase('speaking');
        return;
      }

      if (type === 'response.output_audio_transcript.done') {
        const rid =
          typeof evt.response_id === 'string'
            ? evt.response_id
            : typeof (evt as { response?: { id?: string } }).response?.id === 'string'
              ? (evt as { response: { id: string } }).response.id
              : '';
        const finalT =
          typeof evt.transcript === 'string' && evt.transcript.length > 0
            ? evt.transcript
            : rid
              ? (assistantTranscriptByResponseRef.current.get(rid) ?? '')
              : '';
        if (rid) {
          assistantTranscriptByResponseRef.current.set(rid, finalT);
        }
        setCaptionAssistant(finalT);
        if (rid) {
          onVoiceThreadEventRef.current?.({
            type: 'assistant_transcript_final',
            responseId: rid,
            text: finalT,
          });
        }

        const auth = authRef.current;
        const onCommitted = onTurnCommittedRef.current;
        if (
          auth &&
          finalT.trim().length > 0 &&
          lastUserTranscriptRef.current.trim().length > 0
        ) {
          const token = auth.token;
          const sid = auth.sessionId;
          const userT = lastUserTranscriptRef.current;
          if (!currentTurnClientIdRef.current) {
            try {
              currentTurnClientIdRef.current = crypto.randomUUID();
            } catch {
              currentTurnClientIdRef.current = `turn-${Date.now()}-${Math.random().toString(36).slice(2, 11)}`;
            }
          }
          const clientTurnId = currentTurnClientIdRef.current ?? undefined;
          const openaiResponseId =
            rid && rid.length > 0 ? rid.slice(0, 128) : undefined;
          void commitVoiceTurn(token, sid, userT, finalT, {
            clientTurnId,
            openaiResponseId,
          })
            .then(({ messages, sessionTitle }) => {
              if (authRef.current?.token === token && authRef.current?.sessionId === sid) {
                setCommitErrorMessage(null);
                onCommitted?.({ messages, sessionTitle, chatSessionId: sid });
              }
            })
            .catch((e) => {
              console.warn('[voice] commit failed:', e);
              if (e instanceof VoiceCommitSessionNotFoundError) {
                disconnect();
                setErrorMessage(e.message);
                return;
              }
              const msg = e instanceof Error ? e.message : String(e);
              if (authRef.current?.token === token && authRef.current?.sessionId === sid) {
                setCommitErrorMessage(
                  e instanceof VoiceCommitValidationError
                    ? `Could not save this voice turn: ${msg}`
                    : msg
                );
              }
            });
        }
        return;
      }

      if (type === 'response.function_call_arguments.delta') {
        const callId =
          typeof (evt as { call_id?: string }).call_id === 'string'
            ? (evt as { call_id: string }).call_id
            : '';
        const delta =
          typeof (evt as { delta?: string }).delta === 'string' ? (evt as { delta: string }).delta : '';
        if (callId && delta) {
          const prev = functionCallArgsBufferRef.current.get(callId) ?? '';
          functionCallArgsBufferRef.current.set(callId, prev + delta);
        }
        return;
      }

      if (type === 'response.function_call_arguments.done') {
        const callId =
          typeof (evt as { call_id?: string }).call_id === 'string'
            ? (evt as { call_id: string }).call_id
            : '';
        const name =
          typeof (evt as { name?: string }).name === 'string' ? (evt as { name: string }).name : '';
        if (!callId || handledFunctionCallIdsRef.current.has(callId)) {
          return;
        }
        if (name !== 'lookup_documentation') {
          functionCallArgsBufferRef.current.delete(callId);
          handledFunctionCallIdsRef.current.add(callId);
          const dcUnknown = dcRef.current;
          if (dcUnknown) {
            sendJson(dcUnknown, {
              type: 'conversation.item.create',
              item: {
                type: 'function_call_output',
                call_id: callId,
                output: 'ERROR: unknown tool',
              },
            });
            sendJson(dcUnknown, { type: 'response.create' });
          }
          return;
        }
        let argsJson =
          typeof (evt as { arguments?: string }).arguments === 'string'
            ? (evt as { arguments: string }).arguments
            : '';
        if (!argsJson.trim()) {
          argsJson = functionCallArgsBufferRef.current.get(callId) ?? '';
        }
        functionCallArgsBufferRef.current.delete(callId);
        handledFunctionCallIdsRef.current.add(callId);

        const dc = dcRef.current;
        const auth = authRef.current;
        if (!dc || !auth) return;

        setPhase('tool_lookup');
        enqueueTool(async () => {
          let query = '';
          try {
            const args = JSON.parse(argsJson || '{}') as { query?: unknown };
            query = typeof args.query === 'string' ? args.query : '';
          } catch {
            query = '';
          }
          const resultText = await lookupDocumentationRealtime(
            auth.token,
            auth.sessionId,
            query.trim() || 'documentation'
          );
          sendJson(dc, {
            type: 'conversation.item.create',
            item: {
              type: 'function_call_output',
              call_id: callId,
              output: resultText,
            },
          });
          setPhase('thinking');
          sendJson(dc, { type: 'response.create' });
        });
        return;
      }

      if (type === 'response.created') {
        setPhase('thinking');
        return;
      }

      if (type === 'response.done') {
        const resp = evt.response;
        const rid =
          typeof resp?.id === 'string' && resp.id.length > 0
            ? resp.id
            : typeof (evt as { response_id?: string }).response_id === 'string' &&
                (evt as { response_id: string }).response_id.length > 0
              ? (evt as { response_id: string }).response_id
              : null;
        const output = Array.isArray(resp?.output) ? resp.output : [];
        const lookups = output.filter(
          (
            o
          ): o is {
            type: 'function_call';
            name: string;
            call_id: string;
            arguments: string;
          } =>
            Boolean(
              o &&
                o.type === 'function_call' &&
                o.name === 'lookup_documentation' &&
                typeof o.call_id === 'string' &&
                typeof o.arguments === 'string' &&
                !handledFunctionCallIdsRef.current.has(o.call_id)
            )
        );

        if (lookups.length > 0) {
          setPhase('tool_lookup');
          const dc = dcRef.current;
          const auth = authRef.current;
          if (!dc || !auth) return;

          enqueueTool(async () => {
            for (const item of lookups) {
              handledFunctionCallIdsRef.current.add(item.call_id);
              let query = '';
              try {
                const args = JSON.parse(item.arguments ?? '{}') as { query?: unknown };
                query = typeof args.query === 'string' ? args.query : '';
              } catch {
                query = '';
              }
              const resultText = await lookupDocumentationRealtime(
                auth.token,
                auth.sessionId,
                query.trim() || 'documentation'
              );
              sendJson(dc, {
                type: 'conversation.item.create',
                item: {
                  type: 'function_call_output',
                  call_id: item.call_id,
                  output: resultText,
                },
              });
            }
            setPhase('thinking');
            sendJson(dc, { type: 'response.create' });
          });
        } else {
          setPhase('idle');
          const hadStreamedAssistant =
            sawOutputAudioDeltaRef.current || sawOutputAudioTranscriptDeltaRef.current;
          if (!hadStreamedAssistant) {
            clearAllMicReleaseScheduling();
            releaseMicAfterAssistantPlayback(rid);
          } else if (!playbackTailArmedRef.current) {
            schedulePlaybackTailArmAfterDelay(rid, RESPONSE_DONE_DEFERRED_TAIL_ARM_MS);
          }
        }
        return;
      }

      if (type === 'error' || type === 'invalid_request_error') {
        const errObj = (evt as { error?: { message?: string; code?: string } }).error;
        const msg =
          typeof errObj?.message === 'string'
            ? errObj.message
            : typeof (evt as { message?: string }).message === 'string'
              ? (evt as { message: string }).message
              : 'Realtime error';
        const code = typeof errObj?.code === 'string' ? errObj.code : '';
        /** Benign race when echo triggers VAD during playback — server rejects a duplicate response.create. */
        if (code === 'conversation_already_has_active_response') {
          return;
        }
        setErrorMessage(msg);
        setConnectionState('error');
        setPhase('idle');
        setAssistantPlaybackBlockingMic(false);
        clearAllMicReleaseScheduling();
      }
    },
    [
      armPlaybackTailWatch,
      clearAllMicReleaseScheduling,
      enqueueTool,
      disconnect,
      releaseMicAfterAssistantPlayback,
      schedulePlaybackTailArmAfterDelay,
      setLocalMicEnabled,
    ]
  );

  const connect = useCallback(
    async (accessToken: string, sessionId: string, opts?: VoiceConnectOptions) => {
      disconnect();
      onTurnCommittedRef.current = opts?.onTurnCommitted;
      onVoiceThreadEventRef.current = opts?.onVoiceThreadEvent;
      setErrorMessage(null);
      setCommitErrorMessage(null);
      setConnectionState('connecting');
      setPhase('connecting');
      authRef.current = { token: accessToken, sessionId };

      let mint: Awaited<ReturnType<typeof mintRealtimeSession>>;
      try {
        mint = await mintRealtimeSession(accessToken, sessionId);
      } catch (e) {
        const msg = e instanceof Error ? e.message : 'Failed to mint realtime session';
        setErrorMessage(msg);
        setConnectionState('error');
        setPhase('idle');
        authRef.current = null;
        return;
      }

      const ephemeral = mint.client_secret.value;
      const pc = new RTCPeerConnection({
        iceServers: [{ urls: 'stun:stun.l.google.com:19302' }],
      });
      pcRef.current = pc;

      const remoteAudio = document.createElement('audio');
      remoteAudio.autoplay = true;
      remoteAudio.setAttribute('playsinline', 'true');
      remoteAudio.setAttribute('aria-hidden', 'true');
      /** Detached <audio> elements are unreliable for WebRTC MediaStream playback (silent drop / early stop). */
      remoteAudio.style.position = 'fixed';
      remoteAudio.style.left = '-9999px';
      remoteAudio.style.width = '1px';
      remoteAudio.style.height = '1px';
      document.body.appendChild(remoteAudio);
      remoteAudioRef.current = remoteAudio;

      pc.ontrack = (ev) => {
        const [stream] = ev.streams;
        if (!stream) return;
        remoteAudio.srcObject = stream;
        void remoteAudio.play().catch((e) => {
          console.warn('[voice] remote audio play():', e);
        });
        if (pcRef.current !== pc) return;
        if (playbackGraphRef.current) return;
        /**
         * Tap the remote WebRTC MediaStream (not the <audio> element) so the analyser actually
         * sees decoded speaker audio. `MediaElementAudioSourceNode` does not observe audio routed
         * into an HTMLMediaElement via `srcObject` (WebRTC), which makes silence detection fire
         * immediately after `output_audio.done` — mic reopens before real playback ends.
         */
        const graph = createRemotePlaybackGraphFromStream(stream);
        if (!graph) return;
        playbackGraphRef.current = graph;
        void graph.audioContext.resume().catch(() => {});
        playbackTailWatcherRef.current = createPlaybackTailWatcher({
          analyser: graph.analyser,
          onComplete: () => {
            const rid = pendingPlaybackReleaseResponseIdRef.current;
            pendingPlaybackReleaseResponseIdRef.current = null;
            playbackTailArmedRef.current = false;
            releaseMicAfterAssistantPlaybackRef.current(rid);
          },
        });
      };

      let stream: MediaStream;
      try {
        stream = await navigator.mediaDevices.getUserMedia({
          audio: {
            echoCancellation: true,
            noiseSuppression: true,
            autoGainControl: true,
          },
          video: false,
        });
      } catch (e) {
        let msg = "Couldn't access the microphone.";
        if (e instanceof DOMException && e.name === 'NotAllowedError') {
          msg =
            'Mic access was denied. Allow microphone access in your browser settings to use voice.';
        } else if (e instanceof Error && e.message) {
          msg = e.message;
        }
        setErrorMessage(msg);
        setConnectionState('error');
        setPhase('idle');
        disconnect();
        authRef.current = null;
        return;
      }
      mediaStreamRef.current = stream;
      const [track] = stream.getAudioTracks();
      micTrackRef.current = track ?? null;
      if (track) {
        pc.addTrack(track, stream);
      }

      const dc = pc.createDataChannel('oai-events', { ordered: true });
      dcRef.current = dc;

      /**
       * GA Realtime session shape:
       * - `session.type: 'realtime'` is required.
       * - `turn_detection` moved from `session.turn_detection` to `session.audio.input.turn_detection`.
       * - `max_response_output_tokens` renamed to `max_output_tokens`.
       *
       * Tuning rationale:
       * - `interrupt_response: false` — don't cancel the assistant turn when VAD fires on echo.
       * - Stricter `threshold` / `silence_duration_ms` — reduce false speech detection from speaker bleed
       *   (prevents `output_audio_buffer.cleared` + duplicate response races).
       */
      const sendInitialSessionPreferences = () => {
        sendJson(dc, {
          type: 'session.update',
          session: {
            type: 'realtime',
            audio: {
              input: {
                turn_detection: {
                  type: 'server_vad',
                  interrupt_response: false,
                  threshold: 0.78,
                  prefix_padding_ms: 350,
                  silence_duration_ms: 650,
                },
              },
            },
            max_output_tokens: 'inf',
          },
        });
      };
      if (dc.readyState === 'open') {
        sendInitialSessionPreferences();
      } else {
        dc.addEventListener('open', sendInitialSessionPreferences, { once: true });
      }

      dc.addEventListener('message', (ev) => {
        if (typeof ev.data === 'string') {
          handleRealtimeEvent(ev.data);
        }
      });
      dc.addEventListener('close', () => {
        setConnectionState((s) => (s === 'live' ? 'off' : s));
        setPhase('idle');
      });

      let sdp: string;
      try {
        const offer = await pc.createOffer();
        await pc.setLocalDescription(offer);
        sdp = pc.localDescription?.sdp ?? '';
      } catch (e) {
        const msg = e instanceof Error ? e.message : String(e);
        setErrorMessage(`WebRTC offer failed: ${msg}`);
        setConnectionState('error');
        setPhase('idle');
        disconnect();
        authRef.current = null;
        return;
      }
      if (!sdp) {
        setErrorMessage('WebRTC: missing local SDP');
        setConnectionState('error');
        setPhase('idle');
        disconnect();
        authRef.current = null;
        return;
      }

      let answerSdp: string;
      const sdpPostUrl = openAiRealtimeWebRtcSdpUrl(mint.model ?? '');
      try {
        const r = await fetch(sdpPostUrl, {
          method: 'POST',
          headers: {
            Authorization: `Bearer ${ephemeral}`,
            'Content-Type': 'application/sdp',
          },
          body: sdp,
        });
        const peek = await r.clone().text();
        if (!r.ok) {
          /** Surface the full OpenAI response body in DevTools so SDP / API mismatches are obvious. */
          console.error('[voice] OpenAI Realtime SDP POST failed', {
            url: sdpPostUrl,
            status: r.status,
            statusText: r.statusText,
            body: peek,
          });
          throw new Error(
            `OpenAI Realtime WebRTC failed (${r.status}): ${peek ? peek.slice(0, 400) : r.statusText}`
          );
        }
        answerSdp = peek;
      } catch (e) {
        const msg = e instanceof Error ? e.message : 'Realtime handshake failed';
        console.error('[voice] Realtime handshake error', e);
        setErrorMessage(msg);
        setConnectionState('error');
        setPhase('idle');
        disconnect();
        authRef.current = null;
        return;
      }

      try {
        await pc.setRemoteDescription({ type: 'answer', sdp: answerSdp });
      } catch (e) {
        const msg = e instanceof Error ? e.message : String(e);
        setErrorMessage(`WebRTC answer failed: ${msg}`);
        setConnectionState('error');
        setPhase('idle');
        disconnect();
        authRef.current = null;
        return;
      }
      setConnectionState('live');
      setPhase('idle');
      void playbackGraphRef.current?.audioContext.resume().catch(() => {});
    },
    [disconnect, handleRealtimeEvent]
  );

  releaseMicAfterAssistantPlaybackRef.current = releaseMicAfterAssistantPlayback;

  return {
    connectionState,
    phase,
    errorMessage,
    commitErrorMessage,
    captionUser,
    captionAssistant,
    voiceInputReadyNonce,
    assistantPlaybackBlockingMic,
    connect,
    disconnect,
  };
}
