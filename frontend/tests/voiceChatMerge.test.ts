import { describe, expect, it } from 'vitest';
import {
  cleanupVoiceMessagesAfterStop,
  mergeVoiceCommitMessages,
  reduceVoiceThreadEvent,
} from '../src/voiceChatMerge';
import type { ChatMessageResponse } from '../src/api/commitVoiceTurn';
import type { Message } from '../src/hooks/useChatStream';

describe('voiceChatMerge', () => {
  it('keeps assistant transcript anchored after a user voice row', () => {
    const afterUser = reduceVoiceThreadEvent([], {
      type: 'user_transcript_delta',
      itemId: 'item-1',
      text: 'Tell me about DDS.',
      clientTurnId: 'turn-1',
    });

    const afterAssistant = reduceVoiceThreadEvent(afterUser, {
      type: 'assistant_transcript_delta',
      responseId: 'response-1',
      text: 'DDS helps with digital services.',
      clientTurnId: 'turn-1',
    });

    expect(afterAssistant).toMatchObject([
      {
        role: 'user',
        content: 'Tell me about DDS.',
        source: 'voice',
        optimisticVoice: true,
        voiceClientTurnId: 'turn-1',
        voiceUserStreaming: true,
      },
      {
        role: 'bot',
        content: 'DDS helps with digital services.',
        source: 'voice',
        optimisticVoice: true,
        voiceClientTurnId: 'turn-1',
        voiceAssistantStreaming: true,
      },
    ]);
  });

  it('merges assistant preamble and final answer into one bot row for a voice turn', () => {
    const afterUser = reduceVoiceThreadEvent([], {
      type: 'user_transcript_final',
      text: 'Tell me about disability benefits in California.',
      clientTurnId: 'turn-1',
    });
    const afterPreamble = reduceVoiceThreadEvent(afterUser, {
      type: 'assistant_transcript_final',
      responseId: 'response-preamble',
      text: "Okay, let's look that up.",
      clientTurnId: 'turn-1',
    });
    const afterFinal = reduceVoiceThreadEvent(afterPreamble, {
      type: 'assistant_transcript_delta',
      responseId: 'response-final',
      text: 'DDS provides services and supports, not cash benefits.',
      clientTurnId: 'turn-1',
    });

    expect(afterFinal.filter((m) => m.role === 'user')).toHaveLength(1);
    expect(afterFinal.filter((m) => m.role === 'bot')).toHaveLength(1);
    expect(afterFinal[1]).toMatchObject({
      role: 'bot',
      content: "Okay, let's look that up.\n\nDDS provides services and supports, not cash benefits.",
      voiceClientTurnId: 'turn-1',
      voiceResponseId: 'response-final',
      voiceAssistantStreaming: true,
    });
  });

  it('inserts a user placeholder when assistant transcript arrives first', () => {
    const result = reduceVoiceThreadEvent([{ role: 'bot', content: 'Previous answer.' }], {
      type: 'assistant_transcript_delta',
      responseId: 'response-2',
      text: 'New voice answer.',
      clientTurnId: 'turn-2',
    });

    expect(result).toMatchObject([
      { role: 'bot', content: 'Previous answer.' },
      {
        role: 'user',
        content: 'Listening…',
        source: 'voice',
        optimisticVoice: true,
        voiceClientTurnId: 'turn-2',
        voiceUserStreaming: true,
      },
      {
        role: 'bot',
        content: 'New voice answer.',
        source: 'voice',
        optimisticVoice: true,
        voiceClientTurnId: 'turn-2',
        voiceAssistantStreaming: true,
      },
    ]);
  });

  it('replaces only the optimistic voice tail after commit', () => {
    const previous: Message[] = [
      { role: 'bot', content: 'Persisted context.' },
      { role: 'user', content: 'Draft question', optimisticVoice: true },
      { role: 'bot', content: 'Draft answer', optimisticVoice: true },
    ];
    const committed: ChatMessageResponse[] = [
      { id: 'user-1', role: 'user', content: 'Saved question', source: 'voice' },
      { id: 'assistant-1', role: 'assistant', content: 'Saved answer', source: 'voice' },
    ];

    expect(mergeVoiceCommitMessages(previous, committed)).toEqual([
      { role: 'bot', content: 'Persisted context.' },
      {
        role: 'user',
        content: 'Saved question',
        options: [],
        serverMessageId: 'user-1',
        source: 'voice',
      },
      {
        role: 'bot',
        content: 'Saved answer',
        options: [],
        serverMessageId: 'assistant-1',
        source: 'voice',
      },
    ]);
  });

  it('replaces only the matching voice turn when commits resolve out of order', () => {
    const previous: Message[] = [
      { role: 'user', content: 'First question', optimisticVoice: true, voiceClientTurnId: 'turn-1' },
      { role: 'bot', content: 'First draft', optimisticVoice: true, voiceClientTurnId: 'turn-1' },
      { role: 'user', content: 'Second question', optimisticVoice: true, voiceClientTurnId: 'turn-2' },
      { role: 'bot', content: 'Second draft', optimisticVoice: true, voiceClientTurnId: 'turn-2' },
    ];
    const committed: ChatMessageResponse[] = [
      { id: 'user-1', role: 'user', content: 'Saved first question', source: 'voice' },
      { id: 'assistant-1', role: 'assistant', content: 'Saved first answer', source: 'voice' },
    ];

    expect(mergeVoiceCommitMessages(previous, committed, 'turn-1')).toMatchObject([
      { role: 'user', content: 'Saved first question', serverMessageId: 'user-1' },
      { role: 'bot', content: 'Saved first answer', serverMessageId: 'assistant-1' },
      { role: 'user', content: 'Second question', voiceClientTurnId: 'turn-2' },
      { role: 'bot', content: 'Second draft', voiceClientTurnId: 'turn-2' },
    ]);
  });

  it('does not append the same committed voice messages twice', () => {
    const previous: Message[] = [
      {
        role: 'user',
        content: 'Saved question',
        options: [],
        serverMessageId: 'user-1',
        source: 'voice',
      },
      {
        role: 'bot',
        content: 'Saved answer',
        options: [],
        serverMessageId: 'assistant-1',
        source: 'voice',
      },
    ];
    const committed: ChatMessageResponse[] = [
      { id: 'user-1', role: 'user', content: 'Saved question', source: 'voice' },
      { id: 'assistant-1', role: 'assistant', content: 'Saved answer', source: 'voice' },
    ];

    expect(mergeVoiceCommitMessages(previous, committed)).toBe(previous);
  });

  it('reuses an existing listening stub when speech starts for the same turn', () => {
    const assistantFirst = reduceVoiceThreadEvent([], {
      type: 'assistant_transcript_delta',
      responseId: 'response-early',
      text: 'I found that in DDS documentation.',
      clientTurnId: null,
    });

    const afterSpeechStart = reduceVoiceThreadEvent(assistantFirst, {
      type: 'speech_started',
      clientTurnId: 'turn-late',
    });

    expect(afterSpeechStart.filter((m) => m.role === 'user')).toHaveLength(1);
    expect(afterSpeechStart[0]).toMatchObject({
      role: 'user',
      content: 'Listening…',
      voiceClientTurnId: 'turn-late',
    });
  });

  it('removes empty voice placeholders and freezes partial transcripts after stop', () => {
    const previous: Message[] = [
      { role: 'bot', content: 'Existing answer.' },
      {
        role: 'user',
        content: 'Listening…',
        source: 'voice',
        optimisticVoice: true,
        voiceClientTurnId: 'empty-turn',
        voiceUserStreaming: true,
      },
      {
        role: 'user',
        content: 'What services help my child?',
        source: 'voice',
        optimisticVoice: true,
        voiceClientTurnId: 'partial-turn',
        voiceUserStreaming: true,
      },
      {
        role: 'bot',
        content: 'DDS may provide services through regional centers.',
        source: 'voice',
        optimisticVoice: true,
        voiceClientTurnId: 'partial-turn',
        voiceAssistantStreaming: true,
      },
    ];

    expect(cleanupVoiceMessagesAfterStop(previous)).toMatchObject([
      { role: 'bot', content: 'Existing answer.' },
      {
        role: 'user',
        content: 'What services help my child?',
        voiceClientTurnId: 'partial-turn',
        voiceUserStreaming: false,
      },
      {
        role: 'bot',
        content: 'DDS may provide services through regional centers.',
        voiceClientTurnId: 'partial-turn',
        voiceAssistantStreaming: false,
      },
    ]);
  });
});
