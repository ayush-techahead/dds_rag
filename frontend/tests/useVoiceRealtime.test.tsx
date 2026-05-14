import { act, render, screen, waitFor } from '@testing-library/react';
import { useEffect } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { VoiceConnectOptions } from '../src/hooks/useVoiceRealtime';
import { useVoiceRealtime } from '../src/hooks/useVoiceRealtime';

const realtimeMock = vi.hoisted(() => ({
  agents: [] as Array<{ instructions?: string }>,
  sessions: [] as Array<{
    close: ReturnType<typeof vi.fn>;
    emit: (event: unknown) => void;
    mute: ReturnType<typeof vi.fn>;
  }>,
  RealtimeSession: class {
    agent: unknown;
    options: unknown;
    muted = false;
    handlers = new Map<string, Array<(event: unknown) => void>>();
    transport = {
      on: vi.fn((name: string, cb: (event: unknown) => void) => {
        const existing = this.handlers.get(name) ?? [];
        existing.push(cb);
        this.handlers.set(name, existing);
      }),
    };
    connect = vi.fn(async () => {});
    close = vi.fn();
    mute = vi.fn((muted: boolean) => {
      this.muted = muted;
    });
    on = vi.fn();

    constructor(agent: unknown, options: unknown) {
      this.agent = agent;
      this.options = options;
      realtimeMock.sessions.push(this);
    }

    emit(event: unknown) {
      for (const cb of this.handlers.get('*') ?? []) {
        cb(event);
      }
    }
  },
}));

vi.mock('@openai/agents/realtime', () => ({
  RealtimeAgent: class {
    constructor(config: { instructions?: string }) {
      realtimeMock.agents.push(config);
    }
  },
  RealtimeSession: realtimeMock.RealtimeSession,
  tool: (definition: unknown) => definition,
}));

const mintRealtimeSession = vi.fn();
const commitVoiceTurn = vi.fn();
const lookupDocumentationRealtime = vi.fn();

vi.mock('../src/api/mintRealtimeSession', () => ({
  mintRealtimeSession: (...args: unknown[]) => mintRealtimeSession(...args),
}));

vi.mock('../src/api/commitVoiceTurn', () => ({
  VoiceCommitSessionNotFoundError: class VoiceCommitSessionNotFoundError extends Error {},
  VoiceCommitValidationError: class VoiceCommitValidationError extends Error {},
  commitVoiceTurn: (...args: unknown[]) => commitVoiceTurn(...args),
}));

vi.mock('../src/api/lookupDocumentationRealtime', () => ({
  lookupDocumentationRealtime: (...args: unknown[]) => lookupDocumentationRealtime(...args),
}));

function VoiceHarness({
  onVoiceThreadEvent,
  onTurnCommitted,
}: VoiceConnectOptions) {
  const voice = useVoiceRealtime();
  const { connect, connectionState, commitErrorMessage } = voice;

  useEffect(() => {
    void connect('token-1', 'session-1', {
      onVoiceThreadEvent,
      onTurnCommitted,
    });
  }, [connect, onTurnCommitted, onVoiceThreadEvent]);

  return (
    <div>
      <span data-testid="state">{connectionState}</span>
      <span data-testid="commit-error">{commitErrorMessage}</span>
    </div>
  );
}

describe('useVoiceRealtime', () => {
  beforeEach(() => {
    realtimeMock.agents.length = 0;
    realtimeMock.sessions.length = 0;
    mintRealtimeSession.mockReset();
    commitVoiceTurn.mockReset();
    lookupDocumentationRealtime.mockReset();
    mintRealtimeSession.mockResolvedValue({
      chat_session_id: 'session-1',
      openai_session_id: 'openai-session-1',
      client_secret: { value: 'ek-test', expires_at: 2_000_000_000 },
      model: 'gpt-realtime-2',
      voice_instructions: 'Backend-authored DDS voice instructions.',
    });
    commitVoiceTurn.mockResolvedValue({
      messages: [
        { id: 'user-1', role: 'user', content: 'Tell me about DDS.', source: 'voice' },
        { id: 'assistant-1', role: 'assistant', content: 'Grounded answer.', source: 'voice' },
      ],
      sessionTitle: null,
    });
  });

  it('uses backend-authored realtime instructions from the mint response', async () => {
    render(<VoiceHarness />);

    await waitFor(() => expect(screen.getByTestId('state')).toHaveTextContent('live'));

    expect(realtimeMock.agents[0]?.instructions).toBe('Backend-authored DDS voice instructions.');
  });

  it('normalizes wrapped realtime events and commits response.done transcript fallback', async () => {
    const onVoiceThreadEvent = vi.fn();
    render(<VoiceHarness onVoiceThreadEvent={onVoiceThreadEvent} />);

    await waitFor(() => expect(screen.getByTestId('state')).toHaveTextContent('live'));
    const session = realtimeMock.sessions[0]!;

    act(() => {
      session.emit({ event: { type: 'input_audio_buffer.speech_started' } });
      session.emit({
        data: JSON.stringify({
          type: 'conversation.item.input_audio_transcription.completed',
          item_id: 'input-1',
          transcript: 'Tell me about DDS.',
        }),
      });
      session.emit({
        payload: {
          type: 'response.done',
          response: {
            id: 'response-1',
            output: [
              {
                type: 'message',
                content: [{ type: 'output_audio', transcript: 'Grounded answer.' }],
              },
            ],
          },
        },
      });
    });

    await waitFor(() =>
      expect(commitVoiceTurn).toHaveBeenCalledWith(
        'token-1',
        'session-1',
        'Tell me about DDS.',
        'Grounded answer.',
        expect.objectContaining({
          clientTurnId: expect.any(String),
          openaiResponseId: 'response-1',
        })
      )
    );
    expect(onVoiceThreadEvent).toHaveBeenCalledWith(
      expect.objectContaining({
        type: 'assistant_transcript_final',
        responseId: 'response-1',
        text: 'Grounded answer.',
      })
    );
  });

  it('keeps preamble, tool lookup, and final answer in one turn without phantom speech starts', async () => {
    const onVoiceThreadEvent = vi.fn();
    render(<VoiceHarness onVoiceThreadEvent={onVoiceThreadEvent} />);

    await waitFor(() => expect(screen.getByTestId('state')).toHaveTextContent('live'));
    const session = realtimeMock.sessions[0]!;

    act(() => {
      session.emit({ type: 'input_audio_buffer.speech_started' });
      session.emit({
        type: 'conversation.item.input_audio_transcription.completed',
        item_id: 'input-1',
        transcript: 'Tell me about disability benefits in California.',
      });
      session.emit({
        type: 'response.output_audio_transcript.done',
        response_id: 'response-preamble',
        transcript: "Okay, let's look that up.",
      });
      session.emit({
        type: 'response.function_call_arguments.done',
        response_id: 'response-tool',
      });
      session.emit({
        type: 'response.done',
        response: {
          id: 'response-tool',
          output: [
            {
              type: 'function_call',
              name: 'lookup_documentation',
              call_id: 'call-1',
              arguments: '{"query":"California disability benefits"}',
            },
          ],
        },
      });
      session.emit({ type: 'input_audio_buffer.speech_started' });
      session.emit({
        type: 'response.output_audio_transcript.done',
        response_id: 'response-final',
        transcript: 'DDS provides services and supports, not cash benefits.',
      });
      session.emit({
        type: 'response.done',
        response: {
          id: 'response-final',
          output: [
            {
              type: 'message',
              content: [
                {
                  type: 'output_audio',
                  transcript: 'DDS provides services and supports, not cash benefits.',
                },
              ],
            },
          ],
        },
      });
    });

    await waitFor(() => expect(commitVoiceTurn).toHaveBeenCalledTimes(1));
    expect(commitVoiceTurn).toHaveBeenCalledWith(
      'token-1',
      'session-1',
      'Tell me about disability benefits in California.',
      'DDS provides services and supports, not cash benefits.',
      expect.objectContaining({
        clientTurnId: expect.any(String),
        openaiResponseId: 'response-final',
      })
    );
    expect(
      onVoiceThreadEvent.mock.calls.filter(
        ([event]) => (event as { type?: string }).type === 'speech_started'
      )
    ).toHaveLength(1);
  });

  it('keeps the session live for non-fatal realtime errors', async () => {
    render(<VoiceHarness />);

    await waitFor(() => expect(screen.getByTestId('state')).toHaveTextContent('live'));
    const session = realtimeMock.sessions[0]!;

    act(() => {
      session.emit({
        type: 'invalid_request_error',
        error: { code: 'invalid_value', message: 'Unsupported transient client event.' },
      });
    });

    expect(session.close).not.toHaveBeenCalled();
    expect(screen.getByTestId('state')).toHaveTextContent('live');
    expect(screen.getByTestId('commit-error')).toHaveTextContent('Unsupported transient client event.');
  });
});
