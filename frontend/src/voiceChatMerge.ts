import type { ChatMessageResponse } from './api/commitVoiceTurn';
import type { Message } from './hooks/useChatStream';
import type { VoiceThreadEvent } from './hooks/useVoiceRealtime';

function findLastIndexMessage(arr: Message[], pred: (m: Message) => boolean): number {
  for (let i = arr.length - 1; i >= 0; i--) {
    if (pred(arr[i]!)) return i;
  }
  return -1;
}

/** Placeholder row so assistant streaming always appears after a user row for this turn. */
function pushOptimisticUserStub(n: Message[]): void {
  n.push({
    role: 'user',
    content: '…',
    optimisticVoice: true,
    voiceUserStreaming: true,
  });
}

/**
 * Ensures the list ends with an optimistic user bubble so a new assistant bubble can be
 * appended after it (never `[…, bot]` for a new assistant without a user anchor before it).
 */
function ensureOptimisticUserAnchorBeforeAssistant(n: Message[]): void {
  const last = n[n.length - 1];
  if (!last) {
    pushOptimisticUserStub(n);
    return;
  }
  if (last.role === 'user' && last.optimisticVoice) {
    return;
  }
  if (last.role === 'user' && !last.optimisticVoice) {
    pushOptimisticUserStub(n);
    return;
  }
  if (last.role === 'bot') {
    pushOptimisticUserStub(n);
  }
}

function isUserStubForVoice(m: Message): boolean {
  return (
    m.role === 'user' &&
    Boolean(m.optimisticVoice) &&
    (m.content === 'Listening…' || m.content === '…' || m.content.trim().length === 0)
  );
}

/** Apply one OpenAI Realtime transcript event to the chat message list */
export function reduceVoiceThreadEvent(prev: Message[], ev: VoiceThreadEvent): Message[] {
  switch (ev.type) {
    case 'speech_started': {
      /** User turn starts — do not add a "Listening…" chat row (ordering races with assistant). */
      return prev;
    }
    case 'user_transcript_delta': {
      const n = [...prev];
      let idx = findLastIndexMessage(
        n,
        (m) => m.role === 'user' && Boolean(m.optimisticVoice) && m.voiceItemId === ev.itemId
      );
      if (idx === -1) {
        const last = n[n.length - 1];
        if (last && last.role === 'user' && last.optimisticVoice && isUserStubForVoice(last)) {
          idx = n.length - 1;
          n[idx] = {
            ...last,
            content: ev.text,
            voiceUserStreaming: true,
            voiceItemId: ev.itemId,
          };
          return n;
        }
        const newUser: Message = {
          role: 'user',
          content: ev.text,
          optimisticVoice: true,
          voiceItemId: ev.itemId,
          voiceUserStreaming: true,
        };
        if (last?.role === 'bot' && last.optimisticVoice) {
          n.splice(n.length - 1, 0, newUser);
          return n;
        }
        return [...n, newUser];
      }
      n[idx] = {
        ...n[idx],
        content: ev.text,
        voiceUserStreaming: true,
      };
      return n;
    }
    case 'user_transcript_final': {
      const n = [...prev];
      let i = n.length - 1;
      while (i >= 0 && !(n[i].role === 'user' && n[i].optimisticVoice)) {
        i--;
      }
      if (i >= 0) {
        n[i] = {
          ...n[i],
          content: ev.text,
          optimisticVoice: true,
          voiceUserStreaming: false,
        };
        return n;
      }
      const last = prev[prev.length - 1];
      const appended: Message = { role: 'user', content: ev.text, optimisticVoice: true };
      if (last?.role === 'bot' && last.optimisticVoice) {
        const out = [...prev];
        out.splice(out.length - 1, 0, appended);
        return out;
      }
      return [...prev, appended];
    }
    case 'assistant_transcript_delta': {
      const rid = ev.responseId;
      const n = [...prev];
      const idx = findLastIndexMessage(n, (m) => m.role === 'bot' && m.voiceResponseId === rid);
      if (idx === -1) {
        ensureOptimisticUserAnchorBeforeAssistant(n);
        n.push({
          role: 'bot',
          content: ev.text,
          optimisticVoice: true,
          voiceResponseId: rid,
          voiceAssistantStreaming: true,
        });
        return n;
      }
      n[idx] = {
        ...n[idx],
        content: ev.text,
        voiceAssistantStreaming: true,
      };
      return n;
    }
    case 'assistant_transcript_final': {
      const rid = ev.responseId;
      if (!rid) return prev;
      const n = [...prev];
      const idx = findLastIndexMessage(n, (m) => m.role === 'bot' && m.voiceResponseId === rid);
      if (idx === -1) {
        ensureOptimisticUserAnchorBeforeAssistant(n);
        n.push({
          role: 'bot',
          content: ev.text,
          optimisticVoice: true,
          voiceResponseId: rid,
          voiceAssistantStreaming: false,
        });
        return n;
      }
      n[idx] = {
        ...n[idx],
        content: ev.text,
        voiceAssistantStreaming: false,
      };
      return n;
    }
    default:
      return prev;
  }
}

/** Replace trailing optimistic voice rows with persisted server messages from voice/commit */
export function mergeVoiceCommitMessages(prev: Message[], committed: ChatMessageResponse[]): Message[] {
  let cut = prev.length;
  while (cut > 0 && prev[cut - 1].optimisticVoice) {
    cut--;
  }
  const optimisticTail = prev.length - cut;
  const mapped: Message[] = committed.map((m) => ({
    role: m.role === 'assistant' ? 'bot' : 'user',
    content: m.content,
    options: [],
    ...(typeof m.id === 'string' && m.id.length > 0 ? { serverMessageId: m.id } : {}),
    ...(m.source === 'voice' || m.source === 'text'
      ? { source: m.source as 'voice' | 'text' }
      : {}),
  }));
  if (optimisticTail === 0) {
    return [...prev, ...mapped];
  }
  return [...prev.slice(0, cut), ...mapped];
}
