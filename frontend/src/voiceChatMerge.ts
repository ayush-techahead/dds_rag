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
function pushOptimisticUserStub(n: Message[], clientTurnId?: string | null): void {
  n.push({
    role: 'user',
    content: 'Listening…',
    source: 'voice',
    optimisticVoice: true,
    ...(clientTurnId ? { voiceClientTurnId: clientTurnId } : {}),
    voiceUserStreaming: true,
  });
}

/**
 * Ensures the list ends with an optimistic user bubble so a new assistant bubble can be
 * appended after it (never `[…, bot]` for a new assistant without a user anchor before it).
 */
function ensureOptimisticUserAnchorBeforeAssistant(n: Message[], clientTurnId?: string | null): void {
  if (clientTurnId) {
    const existingTurnUser = n.some(
      (m) => m.role === 'user' && m.optimisticVoice && m.voiceClientTurnId === clientTurnId
    );
    if (existingTurnUser) {
      return;
    }
  }

  const last = n[n.length - 1];
  if (!last) {
    pushOptimisticUserStub(n, clientTurnId);
    return;
  }
  if (
    last.role === 'user' &&
    last.optimisticVoice &&
    (!clientTurnId || !last.voiceClientTurnId || last.voiceClientTurnId === clientTurnId)
  ) {
    if (clientTurnId && !last.voiceClientTurnId) {
      n[n.length - 1] = { ...last, voiceClientTurnId: clientTurnId };
    }
    return;
  }
  if (last.role === 'user' && !last.optimisticVoice) {
    pushOptimisticUserStub(n, clientTurnId);
    return;
  }
  if (last.role === 'bot') {
    pushOptimisticUserStub(n, clientTurnId);
  }
}

function isUserStubForVoice(m: Message): boolean {
  return (
    m.role === 'user' &&
    Boolean(m.optimisticVoice) &&
    (m.content === 'Listening…' || m.content === '…' || m.content.trim().length === 0)
  );
}

function mergeAssistantResponseText(
  message: Message,
  responseId: string,
  text: string,
  streaming: boolean
): Message {
  const responseTexts = {
    ...(message.voiceAssistantResponseTexts ?? {}),
    [responseId]: text,
  };
  return {
    ...message,
    content: Object.values(responseTexts).filter((part) => part.trim().length > 0).join('\n\n'),
    voiceResponseId: responseId,
    voiceAssistantResponseTexts: responseTexts,
    voiceAssistantStreaming: streaming,
  };
}

/** Apply one OpenAI Realtime transcript event to the chat message list */
export function reduceVoiceThreadEvent(prev: Message[], ev: VoiceThreadEvent): Message[] {
  switch (ev.type) {
    case 'speech_started': {
      if (prev.some((m) => m.optimisticVoice && m.voiceClientTurnId === ev.clientTurnId)) {
        return prev;
      }
      const n = [...prev];
      const last = n[n.length - 1];
      if (last && last.role === 'user' && last.optimisticVoice && isUserStubForVoice(last)) {
        n[n.length - 1] = {
          ...last,
          voiceClientTurnId: ev.clientTurnId,
          voiceUserStreaming: true,
        };
        return n;
      }
      const previousUnclaimedStubIdx = findLastIndexMessage(
        n,
        (m) => m.role === 'user' && Boolean(m.optimisticVoice) && !m.voiceClientTurnId && isUserStubForVoice(m)
      );
      if (previousUnclaimedStubIdx >= 0) {
        n[previousUnclaimedStubIdx] = {
          ...n[previousUnclaimedStubIdx],
          voiceClientTurnId: ev.clientTurnId,
          voiceUserStreaming: true,
        };
        return n;
      }
      pushOptimisticUserStub(n, ev.clientTurnId);
      return n;
    }
    case 'user_transcript_delta': {
      const n = [...prev];
      let idx = findLastIndexMessage(
        n,
        (m) =>
          m.role === 'user' &&
          Boolean(m.optimisticVoice) &&
          (m.voiceItemId === ev.itemId ||
            (Boolean(ev.clientTurnId) && m.voiceClientTurnId === ev.clientTurnId))
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
            ...(ev.clientTurnId ? { voiceClientTurnId: ev.clientTurnId } : {}),
          };
          return n;
        }
        const newUser: Message = {
          role: 'user',
          content: ev.text,
          source: 'voice',
          optimisticVoice: true,
          voiceItemId: ev.itemId,
          ...(ev.clientTurnId ? { voiceClientTurnId: ev.clientTurnId } : {}),
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
      if (ev.clientTurnId) {
        const turnIdx = findLastIndexMessage(
          n,
          (m) => m.role === 'user' && Boolean(m.optimisticVoice) && m.voiceClientTurnId === ev.clientTurnId
        );
        if (turnIdx >= 0) {
          i = turnIdx;
        }
      }
      if (i >= 0) {
        n[i] = {
          ...n[i],
          content: ev.text,
          source: 'voice',
          ...(ev.clientTurnId ? { voiceClientTurnId: ev.clientTurnId } : {}),
          optimisticVoice: true,
          voiceUserStreaming: false,
        };
        return n;
      }
      const last = prev[prev.length - 1];
      const appended: Message = {
        role: 'user',
        content: ev.text,
        source: 'voice',
        optimisticVoice: true,
        ...(ev.clientTurnId ? { voiceClientTurnId: ev.clientTurnId } : {}),
      };
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
      let idx = findLastIndexMessage(n, (m) => m.role === 'bot' && m.voiceResponseId === rid);
      if (idx === -1 && ev.clientTurnId) {
        idx = findLastIndexMessage(
          n,
          (m) => m.role === 'bot' && Boolean(m.optimisticVoice) && m.voiceClientTurnId === ev.clientTurnId
        );
      }
      if (idx === -1) {
        ensureOptimisticUserAnchorBeforeAssistant(n, ev.clientTurnId);
        n.push({
          role: 'bot',
          content: ev.text,
          source: 'voice',
          optimisticVoice: true,
          ...(ev.clientTurnId ? { voiceClientTurnId: ev.clientTurnId } : {}),
          voiceResponseId: rid,
          voiceAssistantResponseTexts: { [rid]: ev.text },
          voiceAssistantStreaming: true,
        });
        return n;
      }
      n[idx] = mergeAssistantResponseText(n[idx]!, rid, ev.text, true);
      return n;
    }
    case 'assistant_transcript_final': {
      const rid = ev.responseId;
      if (!rid) return prev;
      const n = [...prev];
      let idx = findLastIndexMessage(n, (m) => m.role === 'bot' && m.voiceResponseId === rid);
      if (idx === -1 && ev.clientTurnId) {
        idx = findLastIndexMessage(
          n,
          (m) => m.role === 'bot' && Boolean(m.optimisticVoice) && m.voiceClientTurnId === ev.clientTurnId
        );
      }
      if (idx === -1) {
        ensureOptimisticUserAnchorBeforeAssistant(n, ev.clientTurnId);
        n.push({
          role: 'bot',
          content: ev.text,
          source: 'voice',
          optimisticVoice: true,
          ...(ev.clientTurnId ? { voiceClientTurnId: ev.clientTurnId } : {}),
          voiceResponseId: rid,
          voiceAssistantResponseTexts: { [rid]: ev.text },
          voiceAssistantStreaming: false,
        });
        return n;
      }
      n[idx] = mergeAssistantResponseText(n[idx]!, rid, ev.text, false);
      return n;
    }
    default:
      return prev;
  }
}

/** Replace optimistic rows for a voice turn with persisted server messages from voice/commit */
export function mergeVoiceCommitMessages(
  prev: Message[],
  committed: ChatMessageResponse[],
  clientTurnId?: string
): Message[] {
  const mapped: Message[] = committed.map((m) => ({
    role: m.role === 'assistant' ? 'bot' : 'user',
    content: m.content,
    options: [],
    ...(typeof m.id === 'string' && m.id.length > 0 ? { serverMessageId: m.id } : {}),
    ...(m.source === 'voice' || m.source === 'text'
      ? { source: m.source as 'voice' | 'text' }
      : {}),
  }));
  if (clientTurnId) {
    const firstTurnIndex = prev.findIndex(
      (m) => m.optimisticVoice && m.voiceClientTurnId === clientTurnId
    );
    if (firstTurnIndex >= 0) {
      let afterTurnIndex = firstTurnIndex;
      while (
        afterTurnIndex < prev.length &&
        prev[afterTurnIndex]?.optimisticVoice &&
        prev[afterTurnIndex]?.voiceClientTurnId === clientTurnId
      ) {
        afterTurnIndex++;
      }
      return [
        ...prev.slice(0, firstTurnIndex),
        ...mapped,
        ...prev.slice(afterTurnIndex),
      ];
    }
  }
  const committedIds = mapped
    .map((m) => m.serverMessageId)
    .filter((id): id is string => typeof id === 'string' && id.length > 0);
  if (
    committedIds.length > 0 &&
    committedIds.every((id) => prev.some((m) => m.serverMessageId === id))
  ) {
    return prev;
  }
  let cut = prev.length;
  while (cut > 0 && prev[cut - 1].optimisticVoice) {
    cut--;
  }
  const optimisticTail = prev.length - cut;
  if (optimisticTail === 0) {
    return [...prev, ...mapped];
  }
  return [...prev.slice(0, cut), ...mapped];
}

/** Remove empty live-only placeholders and stop cursors when the voice overlay closes. */
export function cleanupVoiceMessagesAfterStop(prev: Message[]): Message[] {
  let changed = false;
  const next: Message[] = [];

  for (const message of prev) {
    if (!message.optimisticVoice) {
      next.push(message);
      continue;
    }

    if (isUserStubForVoice(message) || message.content.trim().length === 0) {
      changed = true;
      continue;
    }

    if (message.voiceUserStreaming || message.voiceAssistantStreaming) {
      changed = true;
      next.push({
        ...message,
        voiceUserStreaming: false,
        voiceAssistantStreaming: false,
      });
      continue;
    }

    next.push(message);
  }

  return changed ? next : prev;
}
