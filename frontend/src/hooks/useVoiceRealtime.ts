import { useCallback, useEffect, useRef, useState } from 'react';
import { RealtimeAgent, RealtimeSession, tool, type TransportEvent } from '@openai/agents/realtime';
import { z } from 'zod';
import type { ChatMessageResponse } from '../api/commitVoiceTurn';
import {
  commitVoiceTurn,
  VoiceCommitSessionNotFoundError,
  VoiceCommitValidationError,
} from '../api/commitVoiceTurn';
import { lookupDocumentationRealtime } from '../api/lookupDocumentationRealtime';
import { mintRealtimeSession } from '../api/mintRealtimeSession';
import {
  createPlaybackTailWatcher,
  createRemotePlaybackGraphFromStream,
  PLAYBACK_TAIL_FALLBACK_DELAY_MS,
  RESPONSE_DONE_DEFERRED_TAIL_ARM_MS,
  type PlaybackTailWatcher,
  type RemotePlaybackGraph,
} from '../lib/voiceRemotePlaybackGate';

export type VoiceThreadEvent =
  | { type: 'speech_started'; clientTurnId: string }
  | { type: 'user_transcript_delta'; itemId: string; text: string; clientTurnId: string | null }
  | { type: 'user_transcript_final'; text: string; clientTurnId: string | null }
  | { type: 'assistant_transcript_delta'; responseId: string; text: string; clientTurnId: string | null }
  | { type: 'assistant_transcript_final'; responseId: string; text: string; clientTurnId: string | null };

export type VoiceConnectOptions = {
  onTurnCommitted?: (detail: {
    messages: ChatMessageResponse[];
    sessionTitle: string | null;
    chatSessionId: string;
    clientTurnId: string;
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
  event?: unknown;
  data?: unknown;
  payload?: unknown;
  realtimeEvent?: unknown;
  session?: {
    turn_detection?: {
      type?: string;
      interrupt_response?: boolean;
      create_response?: boolean;
    } | null;
    audio?: {
      input?: {
        turn_detection?: {
          type?: string;
          interrupt_response?: boolean;
          create_response?: boolean;
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
  item_id?: string;
  item?: {
    id?: string;
    type?: string;
    content?: Array<Record<string, unknown>>;
  };
};

type RealtimeTurnDetection = {
  type?: string;
  interrupt_response?: boolean;
  create_response?: boolean;
};

type VoiceCommitSnapshot = {
  token: string;
  sessionId: string;
  userTranscript: string;
  assistantTranscript: string;
  clientTurnId: string;
  openaiResponseId?: string;
  onCommitted?: VoiceConnectOptions['onTurnCommitted'];
};

type VoiceConnectFn = (
  accessToken: string,
  sessionId: string,
  opts?: VoiceConnectOptions
) => Promise<void>;

type RealtimeEventWrapper = {
  type?: string;
  event?: unknown;
  data?: unknown;
  payload?: unknown;
  realtimeEvent?: unknown;
};

type MaybeWebRtcHolder = {
  pc?: RTCPeerConnection;
  peerConnection?: RTCPeerConnection;
  _pc?: RTCPeerConnection;
  connectionState?: {
    peerConnection?: RTCPeerConnection;
    dataChannel?: RTCDataChannel;
    status?: string;
  };
  remoteStream?: MediaStream;
  mediaStream?: MediaStream;
  stream?: MediaStream;
  audioElement?: HTMLAudioElement;
  audio?: HTMLAudioElement;
};

const FALLBACK_VOICE_INSTRUCTIONS =
  'You are the DDS Demo Bot voice assistant. Answer directly in a candid, supportive voice. For DDS factual, procedural, eligibility, services, program, regional center, or contact questions, call lookup_documentation before answering unless the answer is fully covered by the high-level DDS overview, but never narrate that you are checking anything or using retrieval details. Ground answers in tool results, say plainly when the information is not available for the specific question, and include actual DDS website links only when they are useful. Keep responses concise for voice and ask one focused follow-up only when it helps the user move forward.';

function parseRealtimeJson(raw: string): RealtimeEvent | null {
  try {
    const parsed = JSON.parse(raw) as unknown;
    return normalizeRealtimeEvent(parsed);
  } catch {
    return null;
  }
}

function normalizeRealtimeEvent(raw: unknown, depth = 0): RealtimeEvent | null {
  if (depth > 3 || raw == null) return null;
  if (typeof raw === 'string') return parseRealtimeJson(raw);
  if (typeof raw !== 'object') return null;

  const evt = raw as RealtimeEventWrapper;
  if (typeof evt.type === 'string' && evt.type !== 'transport_event') {
    return raw as RealtimeEvent;
  }

  for (const candidate of [evt.realtimeEvent, evt.event, evt.data, evt.payload]) {
    const normalized = normalizeRealtimeEvent(candidate, depth + 1);
    if (normalized) return normalized;
  }

  return typeof evt.type === 'string' ? (raw as RealtimeEvent) : null;
}

function getEventString(evt: RealtimeEvent, key: string): string {
  const value = (evt as unknown as Record<string, unknown>)[key];
  return typeof value === 'string' ? value : '';
}

function collectTranscriptText(value: unknown): string {
  if (!value || typeof value !== 'object') return '';
  const obj = value as Record<string, unknown>;
  const direct = obj.transcript ?? obj.text;
  if (typeof direct === 'string' && direct.trim()) return direct;

  const content = obj.content;
  if (Array.isArray(content)) {
    return content.map(collectTranscriptText).filter(Boolean).join('');
  }

  const output = obj.output;
  if (Array.isArray(output)) {
    return output.map(collectTranscriptText).filter(Boolean).join('');
  }

  return '';
}

function getResponseDoneTranscript(evt: RealtimeEvent): string {
  return collectTranscriptText(evt.response);
}

function responseHasFunctionCall(evt: RealtimeEvent): boolean {
  return Boolean(evt.response?.output?.some((item) => item.type === 'function_call'));
}

function maybeMediaStream(value: unknown): MediaStream | null {
  if (typeof MediaStream === 'undefined') return null;
  return value instanceof MediaStream ? value : null;
}

function maybeAudioElementStream(value: unknown): MediaStream | null {
  if (typeof HTMLAudioElement === 'undefined') return null;
  if (!(value instanceof HTMLAudioElement)) return null;
  return maybeMediaStream(value.srcObject);
}

function getRemoteMediaStreamFromHolder(holder: unknown): MediaStream | null {
  if (!holder || typeof holder !== 'object') return null;
  const h = holder as MaybeWebRtcHolder;
  return (
    maybeMediaStream(h.remoteStream) ||
    maybeMediaStream(h.mediaStream) ||
    maybeMediaStream(h.stream) ||
    maybeAudioElementStream(h.audioElement) ||
    maybeAudioElementStream(h.audio) ||
    null
  );
}

function getPeerConnectionFromHolder(holder: unknown): RTCPeerConnection | null {
  if (typeof RTCPeerConnection === 'undefined' || !holder || typeof holder !== 'object') return null;
  const h = holder as MaybeWebRtcHolder;
  for (const candidate of [h.peerConnection, h.pc, h._pc, h.connectionState?.peerConnection]) {
    if (candidate instanceof RTCPeerConnection) return candidate;
  }
  return null;
}

function getRealtimeErrorCode(value: unknown): string {
  if (!value || typeof value !== 'object') return '';
  const obj = value as Record<string, unknown>;
  const directError = obj.error;
  if (directError && typeof directError === 'object') {
    const err = directError as Record<string, unknown>;
    if (typeof err.code === 'string') return err.code;
    if (err.error && typeof err.error === 'object') {
      const nested = err.error as Record<string, unknown>;
      if (typeof nested.code === 'string') return nested.code;
    }
  }
  if (typeof obj.code === 'string') return obj.code;
  return '';
}

function keepTransientWebRtcDisconnectAlive(session: RealtimeSession) {
  const pc = getPeerConnectionFromHolder(session.transport);
  if (!pc) return;
  pc.onconnectionstatechange = () => {
    if (pc.connectionState === 'failed' || pc.connectionState === 'closed') {
      session.close();
    }
  };
}

function getRemoteMediaStreamFromSession(session: RealtimeSession): MediaStream | null {
  const holders: unknown[] = [session, session.transport];
  for (const holder of holders) {
    const direct = getRemoteMediaStreamFromHolder(holder);
    if (direct) return direct;

    const pc = getPeerConnectionFromHolder(holder);
    const receiverStream = pc
      ?.getReceivers()
      .map((receiver) => receiver.track)
      .filter((track): track is MediaStreamTrack => Boolean(track) && track.kind === 'audio');
    if (receiverStream && receiverStream.length > 0) {
      return new MediaStream(receiverStream);
    }
  }
  return null;
}

function getRealtimeTurnDetection(evt: RealtimeEvent): RealtimeTurnDetection | null {
  return evt.session?.audio?.input?.turn_detection ?? evt.session?.turn_detection ?? null;
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

  const realtimeSessionRef = useRef<RealtimeSession | null>(null);
  const remotePlaybackGraphRef = useRef<RemotePlaybackGraph | null>(null);
  const playbackTailWatcherRef = useRef<PlaybackTailWatcher | null>(null);
  const pendingPlaybackReleaseResponseIdRef = useRef<string | null>(null);
  const playbackTailArmedRef = useRef(false);
  /** True from first assistant transcript/audio delta until `releaseMicAfterAssistantPlayback` (guards speech_started UX). */
  const assistantOutputActiveRef = useRef(false);
  const authRef = useRef<{ token: string; sessionId: string } | null>(null);
  const connectRef = useRef<VoiceConnectFn | null>(null);
  const transportReconnectInFlightRef = useRef(false);
  const lastTransportReconnectAtRef = useRef(0);
  const onTurnCommittedRef = useRef<VoiceConnectOptions['onTurnCommitted'] | undefined>(undefined);
  const onVoiceThreadEventRef = useRef<VoiceConnectOptions['onVoiceThreadEvent'] | undefined>(undefined);
  const assistantTranscriptByResponseRef = useRef<Map<string, string>>(new Map());
  const latestAssistantTranscriptRef = useRef('');
  const latestAssistantResponseIdRef = useRef<string | null>(null);
  const userTranscriptByItemRef = useRef<Map<string, string>>(new Map());
  const latestUserTranscriptRef = useRef('');
  const lastUserTranscriptRef = useRef('');
  const currentTurnClientIdRef = useRef<string | null>(null);
  /** True while one assistant turn is still producing a preamble, tool call, or final answer. */
  const assistantTurnActiveRef = useRef(false);
  /** True after a function call starts until the tool-backed follow-up answer finishes. */
  const awaitingToolFollowupRef = useRef(false);
  const realtimeAutoResponseEnabledRef = useRef(true);
  const committingTurnClientIdsRef = useRef<Set<string>>(new Set());
  const committedTurnClientIdsRef = useRef<Set<string>>(new Set());
  const [commitErrorMessage, setCommitErrorMessage] = useState<string | null>(null);
  /** Bumped when assistant audio finishes and the mic is opened again for the user. */
  const [voiceInputReadyNonce, setVoiceInputReadyNonce] = useState(0);
  /** True while assistant TTS is in progress; the mic track stays live so WebRTC/VAD remains stable. */
  const [assistantPlaybackBlockingMic, setAssistantPlaybackBlockingMic] = useState(false);

  const sawOutputAudioDeltaRef = useRef(false);
  const sawOutputAudioTranscriptDeltaRef = useRef(false);
  const deferredMicReleaseTimerRef = useRef<number | null>(null);
  /** `response_id`s for which we already ran post-assistant mic release + cue */
  const releasedMicForResponseIdsRef = useRef<Set<string>>(new Set());

  const clearAllMicReleaseScheduling = useCallback(() => {
    if (deferredMicReleaseTimerRef.current !== null) {
      clearTimeout(deferredMicReleaseTimerRef.current);
      deferredMicReleaseTimerRef.current = null;
    }
    playbackTailWatcherRef.current?.cancel();
    pendingPlaybackReleaseResponseIdRef.current = null;
    playbackTailArmedRef.current = false;
  }, []);

  const disposeRemotePlaybackGate = useCallback(() => {
    playbackTailWatcherRef.current?.cancel();
    playbackTailWatcherRef.current = null;
    remotePlaybackGraphRef.current?.dispose();
    remotePlaybackGraphRef.current = null;
  }, []);

  const setSessionMuted = useCallback((muted: boolean) => {
    const session = realtimeSessionRef.current;
    if (!session || session.muted === null) return;
    if (session.muted === muted) return;
    try {
      session.mute(muted);
    } catch {
      /* Some transports do not support mute; WebRTC does. */
    }
  }, []);

  const setRealtimeAutoResponseEnabled = useCallback((enabled: boolean, _reason: string) => {
    const session = realtimeSessionRef.current;
    if (!session || session.transport.status !== 'connected') return;
    if (realtimeAutoResponseEnabledRef.current === enabled) return;
    realtimeAutoResponseEnabledRef.current = enabled;
    try {
      session.transport.updateSessionConfig({
        audio: {
          input: {
            transcription: { model: 'gpt-4o-mini-transcribe' },
            noiseReduction: { type: 'near_field' },
            turnDetection: {
              type: 'semantic_vad',
              eagerness: 'medium',
              createResponse: enabled,
              interruptResponse: false,
            },
          },
        },
      });
    } catch {
      /* Session may be closing or reconnecting; the next session starts with auto response enabled. */
    }
  }, []);

  const startVoiceCommit = useCallback((snapshot: VoiceCommitSnapshot): boolean => {
    if (
      committingTurnClientIdsRef.current.has(snapshot.clientTurnId) ||
      committedTurnClientIdsRef.current.has(snapshot.clientTurnId)
    ) {
      return true;
    }

    committingTurnClientIdsRef.current.add(snapshot.clientTurnId);
    void commitVoiceTurn(
      snapshot.token,
      snapshot.sessionId,
      snapshot.userTranscript,
      snapshot.assistantTranscript,
      {
        clientTurnId: snapshot.clientTurnId,
        openaiResponseId: snapshot.openaiResponseId,
      }
    )
      .then(({ messages, sessionTitle }) => {
        committedTurnClientIdsRef.current.add(snapshot.clientTurnId);
        const currentAuth = authRef.current;
        if (
          !currentAuth ||
          (currentAuth.token === snapshot.token && currentAuth.sessionId === snapshot.sessionId)
        ) {
          setCommitErrorMessage(null);
        }
        snapshot.onCommitted?.({
          messages,
          sessionTitle,
          chatSessionId: snapshot.sessionId,
          clientTurnId: snapshot.clientTurnId,
        });
      })
      .catch((e) => {
        console.warn('[voice] commit failed:', e);
        if (e instanceof VoiceCommitSessionNotFoundError) {
          setErrorMessage(e.message);
          return;
        }
        const msg = e instanceof Error ? e.message : String(e);
        if (
          authRef.current?.token === snapshot.token &&
          authRef.current?.sessionId === snapshot.sessionId
        ) {
          setCommitErrorMessage(
            e instanceof VoiceCommitValidationError
              ? `Could not save this voice turn: ${msg}`
              : msg
          );
        }
      })
      .finally(() => {
        committingTurnClientIdsRef.current.delete(snapshot.clientTurnId);
      });

    return true;
  }, []);

  const flushCurrentVoiceTurn = useCallback(
    (assistantTranscriptOverride?: string, responseIdOverride?: string | null): boolean => {
      const auth = authRef.current;
      if (!auth) return false;

      const userTranscript = (lastUserTranscriptRef.current || latestUserTranscriptRef.current).trim();
      const assistantTranscript = (
        assistantTranscriptOverride ||
        latestAssistantTranscriptRef.current ||
        (latestAssistantResponseIdRef.current
          ? assistantTranscriptByResponseRef.current.get(latestAssistantResponseIdRef.current)
          : '') ||
        ''
      ).trim();
      if (!userTranscript || !assistantTranscript) return false;

      if (!currentTurnClientIdRef.current) {
        try {
          currentTurnClientIdRef.current = crypto.randomUUID();
        } catch {
          currentTurnClientIdRef.current = `turn-${Date.now()}-${Math.random().toString(36).slice(2, 11)}`;
        }
      }

      const responseId = responseIdOverride || latestAssistantResponseIdRef.current || '';
      return startVoiceCommit({
        token: auth.token,
        sessionId: auth.sessionId,
        userTranscript,
        assistantTranscript,
        clientTurnId: currentTurnClientIdRef.current,
        openaiResponseId: responseId.length > 0 ? responseId.slice(0, 128) : undefined,
        onCommitted: onTurnCommittedRef.current,
      });
    },
    [startVoiceCommit]
  );

  const releaseMicAfterAssistantPlayback = useCallback(
    (responseId: string | null) => {
      if (!authRef.current) return;
      const rid = responseId && responseId.length > 0 ? responseId : 'unknown-response';
      if (releasedMicForResponseIdsRef.current.has(rid)) return;
      releasedMicForResponseIdsRef.current.add(rid);
      clearAllMicReleaseScheduling();
      if (awaitingToolFollowupRef.current) {
        setAssistantPlaybackBlockingMic(true);
        assistantOutputActiveRef.current = true;
        assistantTurnActiveRef.current = true;
        setRealtimeAutoResponseEnabled(false, 'awaiting_tool_followup_release');
        setPhase('tool_lookup');
        return;
      }
      setSessionMuted(false);
      setRealtimeAutoResponseEnabled(true, 'assistant_playback_released');
      setAssistantPlaybackBlockingMic(false);
      assistantOutputActiveRef.current = false;
      assistantTurnActiveRef.current = false;
      setPhase('idle');
      setVoiceInputReadyNonce((n) => n + 1);
      playVoiceInputReadyChime();
      sawOutputAudioDeltaRef.current = false;
      sawOutputAudioTranscriptDeltaRef.current = false;
    },
    [clearAllMicReleaseScheduling, setRealtimeAutoResponseEnabled, setSessionMuted]
  );

  const scheduleTransportReconnect = useCallback(
    (_reason: string): boolean => {
      const auth = authRef.current;
      const reconnect = connectRef.current;
      const now = Date.now();
      const canReconnect =
        Boolean(auth) &&
        Boolean(reconnect) &&
        !transportReconnectInFlightRef.current &&
        now - lastTransportReconnectAtRef.current > 1500;
      if (!canReconnect || !auth || !reconnect) return false;

      transportReconnectInFlightRef.current = true;
      lastTransportReconnectAtRef.current = now;
      setConnectionState('connecting');
      setPhase('connecting');
      setAssistantPlaybackBlockingMic(false);
      assistantOutputActiveRef.current = false;
      assistantTurnActiveRef.current = false;
      awaitingToolFollowupRef.current = false;
      realtimeAutoResponseEnabledRef.current = true;
      clearAllMicReleaseScheduling();
      void reconnect(auth.token, auth.sessionId, {
        onTurnCommitted: onTurnCommittedRef.current,
        onVoiceThreadEvent: onVoiceThreadEventRef.current,
      }).finally(() => {
        transportReconnectInFlightRef.current = false;
      });
      return true;
    },
    [clearAllMicReleaseScheduling]
  );

  const setupRemotePlaybackGate = useCallback(
    (session: RealtimeSession) => {
      disposeRemotePlaybackGate();
      const stream = getRemoteMediaStreamFromSession(session);
      if (!stream) {
        return false;
      }
      const graph = createRemotePlaybackGraphFromStream(stream);
      if (!graph) {
        return false;
      }

      remotePlaybackGraphRef.current = graph;
      playbackTailWatcherRef.current = createPlaybackTailWatcher({
        analyser: graph.analyser,
        onComplete: () => {
          const responseId = pendingPlaybackReleaseResponseIdRef.current;
          releaseMicAfterAssistantPlayback(responseId);
        },
      });
      const pc = getPeerConnectionFromHolder(session.transport);
      keepTransientWebRtcDisconnectAlive(session);
      if (pc) {
        pc.addEventListener('connectionstatechange', () => {
          if (pc.connectionState === 'disconnected') {
            scheduleTransportReconnect('peer_connection_disconnected');
          }
        });
        pc.addEventListener('iceconnectionstatechange', () => {
          if (pc.iceConnectionState === 'disconnected') {
            scheduleTransportReconnect('ice_connection_disconnected');
          }
        });
      }
      return true;
    },
    [disposeRemotePlaybackGate, releaseMicAfterAssistantPlayback, scheduleTransportReconnect]
  );

  const scheduleDeferredMicRelease = useCallback(
    (responseId: string | null, delayMs: number) => {
      playbackTailArmedRef.current = true;
      pendingPlaybackReleaseResponseIdRef.current = responseId;
      playbackTailWatcherRef.current?.cancel();
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
      if (deferredMicReleaseTimerRef.current !== null) {
        clearTimeout(deferredMicReleaseTimerRef.current);
        deferredMicReleaseTimerRef.current = null;
      }
      pendingPlaybackReleaseResponseIdRef.current = responseId;
      playbackTailArmedRef.current = true;
      if (playbackTailWatcherRef.current) {
        playbackTailWatcherRef.current.arm();
        return;
      }
      scheduleDeferredMicRelease(responseId, PLAYBACK_TAIL_FALLBACK_DELAY_MS);
    },
    [scheduleDeferredMicRelease]
  );

  /** `response.done` can race ahead of `output_audio.done` / local audio — wait before arming RMS tail. */
  const schedulePlaybackTailArmAfterDelay = useCallback(
    (responseId: string | null, delayMs: number) => {
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

  const disconnect = useCallback((_reason = 'manual_or_lifecycle') => {
    flushCurrentVoiceTurn();
    authRef.current = null;
    transportReconnectInFlightRef.current = false;
    setPhase('idle');
    setConnectionState('off');
    setCaptionAssistant('');
    setCaptionUser('');
    setCommitErrorMessage(null);
    setAssistantPlaybackBlockingMic(false);
    clearAllMicReleaseScheduling();
    disposeRemotePlaybackGate();
    assistantOutputActiveRef.current = false;
    assistantTurnActiveRef.current = false;
    awaitingToolFollowupRef.current = false;
    realtimeAutoResponseEnabledRef.current = true;
    sawOutputAudioDeltaRef.current = false;
    sawOutputAudioTranscriptDeltaRef.current = false;
    releasedMicForResponseIdsRef.current.clear();
    setVoiceInputReadyNonce(0);
    currentTurnClientIdRef.current = null;
    latestAssistantTranscriptRef.current = '';
    latestAssistantResponseIdRef.current = null;
    latestUserTranscriptRef.current = '';
    lastUserTranscriptRef.current = '';

    if (realtimeSessionRef.current) {
      try {
        realtimeSessionRef.current.close();
      } catch {
        /* ignore */
      }
      realtimeSessionRef.current = null;
    }
    onTurnCommittedRef.current = undefined;
    onVoiceThreadEventRef.current = undefined;
  }, [clearAllMicReleaseScheduling, disposeRemotePlaybackGate, flushCurrentVoiceTurn]);

  const handleRealtimeEvent = useCallback(
    (raw: unknown) => {
      const evt = normalizeRealtimeEvent(raw);
      if (!evt) return;
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
                : typeRaw === 'conversation.item.input_audio_transcription.done'
                  ? 'conversation.item.input_audio_transcription.completed'
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
        latestUserTranscriptRef.current = next;
        setCaptionUser(next);
        onVoiceThreadEventRef.current?.({
          type: 'user_transcript_delta',
          itemId,
          text: next,
          clientTurnId: currentTurnClientIdRef.current,
        });
        return;
      }

      if (type === 'conversation.item.input_audio_transcription.completed') {
        const itemId = getEventString(evt, 'item_id') || evt.item?.id || 'default';
        const eventTranscript = typeof evt.transcript === 'string' ? evt.transcript : '';
        const t = eventTranscript || userTranscriptByItemRef.current.get(itemId) || '';
        latestUserTranscriptRef.current = t;
        lastUserTranscriptRef.current = t;
        setCaptionUser(t);
        onVoiceThreadEventRef.current?.({
          type: 'user_transcript_final',
          text: t,
          clientTurnId: currentTurnClientIdRef.current,
        });
        return;
      }

      if (type === 'input_audio_buffer.speech_started') {
        if (
          assistantTurnActiveRef.current ||
          assistantOutputActiveRef.current ||
          playbackTailArmedRef.current ||
          awaitingToolFollowupRef.current
        ) {
          return;
        }
        userTranscriptByItemRef.current.clear();
        latestUserTranscriptRef.current = '';
        lastUserTranscriptRef.current = '';
        setCaptionUser('Listening…');
        setCaptionAssistant('');
        latestAssistantTranscriptRef.current = '';
        latestAssistantResponseIdRef.current = null;
        assistantTurnActiveRef.current = false;
        awaitingToolFollowupRef.current = false;
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
        onVoiceThreadEventRef.current?.({
          type: 'speech_started',
          clientTurnId: currentTurnClientIdRef.current,
        });
        return;
      }
      if (type === 'input_audio_buffer.speech_stopped') {
        if (
          assistantTurnActiveRef.current ||
          assistantOutputActiveRef.current ||
          playbackTailArmedRef.current ||
          awaitingToolFollowupRef.current
        ) {
          return;
        }
        setPhase('thinking');
        return;
      }

      if (type === 'response.output_audio.delta') {
        clearAllMicReleaseScheduling();
        sawOutputAudioDeltaRef.current = true;
        assistantOutputActiveRef.current = true;
        assistantTurnActiveRef.current = true;
        setRealtimeAutoResponseEnabled(false, 'assistant_audio_delta');
        setAssistantPlaybackBlockingMic(true);
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
        assistantTurnActiveRef.current = true;
        setRealtimeAutoResponseEnabled(false, 'assistant_audio_transcript_delta');
        setAssistantPlaybackBlockingMic(true);
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
          latestAssistantResponseIdRef.current = rid;
          latestAssistantTranscriptRef.current = next;
          setCaptionAssistant(next);
          onVoiceThreadEventRef.current?.({
            type: 'assistant_transcript_delta',
            responseId: rid,
            text: next,
            clientTurnId: currentTurnClientIdRef.current,
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
          latestAssistantResponseIdRef.current = rid;
        }
        latestAssistantTranscriptRef.current = finalT;
        setCaptionAssistant(finalT);
        if (rid) {
          onVoiceThreadEventRef.current?.({
            type: 'assistant_transcript_final',
            responseId: rid,
            text: finalT,
            clientTurnId: currentTurnClientIdRef.current,
          });
        }
        return;
      }

      if (type === 'response.function_call_arguments.delta' || type === 'response.function_call_arguments.done') {
        awaitingToolFollowupRef.current = true;
        assistantTurnActiveRef.current = true;
        assistantOutputActiveRef.current = true;
        setRealtimeAutoResponseEnabled(false, 'function_call_arguments');
        setAssistantPlaybackBlockingMic(true);
        setPhase('tool_lookup');
        return;
      }

      if (type === 'response.created') {
        assistantTurnActiveRef.current = true;
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
        if (responseHasFunctionCall(evt)) {
          awaitingToolFollowupRef.current = true;
          assistantTurnActiveRef.current = true;
          assistantOutputActiveRef.current = true;
          setRealtimeAutoResponseEnabled(false, 'response_done_function_call');
          setAssistantPlaybackBlockingMic(true);
          setPhase('tool_lookup');
          return;
        }
        awaitingToolFollowupRef.current = false;
        const doneTranscript = (
          getResponseDoneTranscript(evt) ||
          latestAssistantTranscriptRef.current ||
          (rid ? assistantTranscriptByResponseRef.current.get(rid) : '') ||
          ''
        ).trim();
        if (doneTranscript) {
          const useRid = rid || 'response-done';
          assistantTranscriptByResponseRef.current.set(useRid, doneTranscript);
          latestAssistantResponseIdRef.current = useRid;
          latestAssistantTranscriptRef.current = doneTranscript;
          setCaptionAssistant(doneTranscript);
          onVoiceThreadEventRef.current?.({
            type: 'assistant_transcript_final',
            responseId: useRid,
            text: doneTranscript,
            clientTurnId: currentTurnClientIdRef.current,
          });
          flushCurrentVoiceTurn(doneTranscript, useRid);
        }
        setPhase('idle');
        const hadStreamedAssistant =
          sawOutputAudioDeltaRef.current || sawOutputAudioTranscriptDeltaRef.current;
        if (!hadStreamedAssistant) {
          clearAllMicReleaseScheduling();
          releaseMicAfterAssistantPlayback(rid);
        } else if (!playbackTailArmedRef.current) {
          schedulePlaybackTailArmAfterDelay(rid, RESPONSE_DONE_DEFERRED_TAIL_ARM_MS);
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
        const fatalCodes = new Set([
          'authentication_error',
          'expired_token',
          'invalid_api_key',
          'invalid_client_secret',
          'session_expired',
          'session_not_found',
        ]);
        if (!fatalCodes.has(code)) {
          console.warn('[voice] non-fatal realtime error:', evt);
          setCommitErrorMessage(msg);
          return;
        }
        disconnect('fatal_realtime_error');
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
      disconnect,
      flushCurrentVoiceTurn,
      releaseMicAfterAssistantPlayback,
      schedulePlaybackTailArmAfterDelay,
      setRealtimeAutoResponseEnabled,
      setSessionMuted,
    ]
  );

  const connect = useCallback(
    async (accessToken: string, sessionId: string, opts?: VoiceConnectOptions) => {
      disconnect('connect_reset');
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

      const lookupDocumentationTool = tool({
        name: 'lookup_documentation',
        description:
          'Retrieve relevant DDS context for answering the user.',
        parameters: z.object({
          query: z.string().describe('The concise DDS lookup query.'),
        }),
        async execute({ query }) {
          const auth = authRef.current;
          if (!auth) {
            return 'NO_RELEVANT_INFO: The information is not available because the voice session is no longer active.';
          }
          setPhase('tool_lookup');
          try {
            const result = await lookupDocumentationRealtime(
              auth.token,
              auth.sessionId,
              query.trim() || 'documentation'
            );
            return result;
          } catch (e) {
            const msg = e instanceof Error ? e.message : String(e);
            console.error('[voice] documentation lookup failed', e);
            setCommitErrorMessage(`Could not prepare this voice answer: ${msg}`);
            return `NO_RELEVANT_INFO: The information is not available right now.`;
          } finally {
            setPhase('thinking');
          }
        },
      });

      const agent = new RealtimeAgent({
        name: 'DDS Voice Assistant',
        instructions: mint.voice_instructions?.trim() || FALLBACK_VOICE_INSTRUCTIONS,
        tools: [lookupDocumentationTool],
      });
      const session = new RealtimeSession(agent, {
        model: mint.model || 'gpt-realtime-2',
        transport: 'webrtc',
        config: {
          outputModalities: ['audio'],
          providerData: {
            max_output_tokens: 'inf',
          },
          reasoning: { effort: 'low' },
          parallelToolCalls: false,
          audio: {
            input: {
              transcription: { model: 'gpt-4o-mini-transcribe' },
              noiseReduction: { type: 'near_field' },
              turnDetection: {
                type: 'semantic_vad',
                eagerness: 'medium',
                createResponse: true,
                interruptResponse: false,
              },
            },
          },
        },
      });
      realtimeSessionRef.current = session;
      session.transport.on('*', (event: TransportEvent) => {
        handleRealtimeEvent(event as RealtimeEvent);
      });
      session.transport.on('connection_change', (nextState) => {
        const canReconnect =
          nextState === 'disconnected' &&
          realtimeSessionRef.current === session &&
          authRef.current?.token === accessToken &&
          authRef.current.sessionId === sessionId;
        if (canReconnect) {
          scheduleTransportReconnect('transport_disconnected');
          return;
        }
        if (nextState === 'disconnected') {
          setConnectionState((s) => (s === 'live' || s === 'connecting' ? 'off' : s));
          setPhase('idle');
        }
      });
      session.on('error', (err) => {
        const code = getRealtimeErrorCode(err);
        if (code === 'conversation_already_has_active_response') {
          return;
        }
        console.error('[voice] realtime session error', err);
        setErrorMessage('Realtime voice session failed. Please try again.');
        setConnectionState('error');
        setPhase('idle');
      });

      try {
        await session.connect({
          apiKey: mint.client_secret.value,
          model: mint.model || 'gpt-realtime-2',
        });
      } catch (e) {
        const msg = e instanceof Error ? e.message : 'Realtime voice session failed';
        console.error('[voice] Realtime session connect error', e);
        setErrorMessage(msg);
        setConnectionState('error');
        setPhase('idle');
        disconnect('connect_error');
        authRef.current = null;
        return;
      }
      if (!setupRemotePlaybackGate(session)) {
        window.setTimeout(() => {
          if (realtimeSessionRef.current === session) {
            setupRemotePlaybackGate(session);
          }
        }, 250);
      }
      setConnectionState('live');
      setPhase('idle');
    },
    [
      clearAllMicReleaseScheduling,
      disconnect,
      handleRealtimeEvent,
      scheduleTransportReconnect,
      setupRemotePlaybackGate,
    ]
  );

  connectRef.current = connect;

  useEffect(() => {
    const closeLiveSession = () => {
      if (authRef.current) {
        disconnect('pagehide');
      }
    };
    window.addEventListener('pagehide', closeLiveSession);
    return () => {
      window.removeEventListener('pagehide', closeLiveSession);
    };
  }, [disconnect]);

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
