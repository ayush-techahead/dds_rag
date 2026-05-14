/**
 * Gates local mic reopen on **audible** end of assistant WebRTC playback.
 * OpenAI may emit `output_audio.done` before the browser finishes decoding/playing the tail.
 */

/** Float time-domain samples: treat as silence below this RMS (tune per device; lower = stricter “silent”). */
export const PLAYBACK_TAIL_RMS_THRESHOLD = 0.006;

/** Require this many ms of sub-threshold RMS before declaring playback finished. */
export const PLAYBACK_TAIL_SILENCE_MS = 550;

/** Ignore silence detection for this long after arming (small jitter buffer; the analyser now sees real audio). */
export const PLAYBACK_TAIL_MIN_HOLD_MS = 180;

/** Force mic release if silence never stabilizes (analyser can read residual WebRTC noise). */
export const PLAYBACK_TAIL_MAX_MS = 1800;

/** Polling interval for RMS reads (stable under background tab throttling vs rAF). */
export const PLAYBACK_TAIL_SAMPLE_INTERVAL_MS = 32;

/** When Web Audio graph cannot be built, fall back to this fixed delay after `output_audio.done`. */
export const PLAYBACK_TAIL_FALLBACK_DELAY_MS = 2400;

/**
 * `response.done` often arrives with transcript completion, **before** `output_audio.done` or while
 * decoded audio has not reached the analyser yet. Defer arming the silence tail by this much when we
 * only have `response.done` so we do not treat pre-audio idle as “playback finished”.
 */
export const RESPONSE_DONE_DEFERRED_TAIL_ARM_MS = 750;

export function computeRmsFromFloatTimeDomainData(data: Float32Array): number {
  const n = data.length;
  if (n === 0) return 0;
  let sum = 0;
  for (let i = 0; i < n; i++) {
    const s = data[i]!;
    sum += s * s;
  }
  return Math.sqrt(sum / n);
}

export type RemotePlaybackGraph = {
  audioContext: AudioContext;
  analyser: AnalyserNode;
  dispose: () => void;
};

/**
 * Tap the **WebRTC remote MediaStream** with Web Audio so we can meter the same audio the user hears.
 *
 * Why a `MediaStreamAudioSourceNode` and not `createMediaElementSource(audioEl)`:
 * when an `HTMLMediaElement` plays a `MediaStream` via `srcObject` (the WebRTC case),
 * `MediaElementAudioSourceNode` does not reliably observe that audio in most browsers — the
 * analyser reads silence the whole time, so any silence-based tail detection fires prematurely
 * (right around when transcripts finish, before the speaker tail ends).
 *
 * The analyser is **not** connected to `destination` — the `<audio>` element already plays the
 * stream; connecting here would double-play. We only need the analyser for RMS.
 */
export function createRemotePlaybackGraphFromStream(
  stream: MediaStream
): RemotePlaybackGraph | null {
  try {
    const AC =
      typeof AudioContext !== 'undefined'
        ? AudioContext
        : (typeof window !== 'undefined'
            ? (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext
            : undefined);
    if (!AC) return null;
    if (stream.getAudioTracks().length === 0) return null;

    const audioContext = new AC();
    const source = audioContext.createMediaStreamSource(stream);
    const analyser = audioContext.createAnalyser();
    analyser.fftSize = 2048;
    analyser.smoothingTimeConstant = 0.35;
    source.connect(analyser);

    return {
      audioContext,
      analyser,
      dispose: () => {
        try {
          source.disconnect();
        } catch {
          /* ignore */
        }
        try {
          analyser.disconnect();
        } catch {
          /* ignore */
        }
        void audioContext.close().catch(() => {});
      },
    };
  } catch {
    return null;
  }
}

export type PlaybackTailWatcher = {
  arm: () => void;
  cancel: () => void;
  readonly armed: boolean;
};

export function createPlaybackTailWatcher(opts: {
  analyser: AnalyserNode;
  onComplete: () => void;
  rmsThreshold?: number;
  silenceDurationMs?: number;
  minHoldAfterArmMs?: number;
  maxWaitMs?: number;
  sampleIntervalMs?: number;
}): PlaybackTailWatcher {
  let intervalId: number | null = null;
  let armed = false;
  let armedAt = 0;
  let silenceAccumMs = 0;
  const buffer = new Float32Array(opts.analyser.fftSize);
  const threshold = opts.rmsThreshold ?? PLAYBACK_TAIL_RMS_THRESHOLD;
  const minHold = opts.minHoldAfterArmMs ?? PLAYBACK_TAIL_MIN_HOLD_MS;
  const needSilence = opts.silenceDurationMs ?? PLAYBACK_TAIL_SILENCE_MS;
  const maxWait = opts.maxWaitMs ?? PLAYBACK_TAIL_MAX_MS;
  const sampleInterval = opts.sampleIntervalMs ?? PLAYBACK_TAIL_SAMPLE_INTERVAL_MS;

  const clear = () => {
    if (intervalId !== null) {
      clearInterval(intervalId);
      intervalId = null;
    }
    armed = false;
    silenceAccumMs = 0;
  };

  const tick = () => {
    if (!armed) return;
    const now = performance.now();
    const elapsed = now - armedAt;
    if (elapsed >= maxWait) {
      clear();
      opts.onComplete();
      return;
    }

    opts.analyser.getFloatTimeDomainData(buffer);
    const rms = computeRmsFromFloatTimeDomainData(buffer);

    if (elapsed < minHold) {
      return;
    }

    if (rms < threshold) {
      silenceAccumMs += sampleInterval;
      if (silenceAccumMs >= needSilence) {
        clear();
        opts.onComplete();
      }
    } else {
      silenceAccumMs = 0;
    }
  };

  return {
    arm() {
      clear();
      armed = true;
      armedAt = performance.now();
      silenceAccumMs = 0;
      intervalId = window.setInterval(tick, sampleInterval);
    },
    cancel() {
      clear();
    },
    get armed() {
      return armed;
    },
  };
}
