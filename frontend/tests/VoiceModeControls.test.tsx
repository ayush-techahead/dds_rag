import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { VoiceModeControls, type VoiceModeControlsProps } from '../src/components/VoiceModeControls';

function renderVoiceControls(overrides: Partial<VoiceModeControlsProps> = {}) {
  const props: VoiceModeControlsProps = {
    connectionState: 'off',
    phase: 'idle',
    captionUser: '',
    captionAssistant: '',
    voiceInputReadyNonce: 0,
    errorMessage: null,
    commitErrorMessage: null,
    assistantPlaybackBlockingMic: false,
    disabled: false,
    onStart: vi.fn(),
    onStop: vi.fn(),
    ...overrides,
  };

  render(<VoiceModeControls {...props} />);
  return props;
}

describe('VoiceModeControls', () => {
  it('starts voice from the idle mic button', () => {
    const props = renderVoiceControls();

    fireEvent.click(screen.getByRole('button', { name: /start voice session/i }));

    expect(props.onStart).toHaveBeenCalledTimes(1);
    expect(props.onStop).not.toHaveBeenCalled();
  });

  it('keeps end call available while assistant playback is active', () => {
    const props = renderVoiceControls({
      connectionState: 'live',
      phase: 'speaking',
      captionAssistant: 'Here is what DDS can do for you.',
      assistantPlaybackBlockingMic: true,
    });

    expect(screen.getByText('Speaking')).toBeInTheDocument();
    expect(screen.getByText(/ask your follow-up after the ready chime/i)).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /end voice session/i }));

    expect(props.onStop).toHaveBeenCalledTimes(1);
    expect(props.onStart).not.toHaveBeenCalled();
  });

  it('renders phase labels, live captions, ready hints, and errors', () => {
    renderVoiceControls({
      connectionState: 'live',
      phase: 'tool_lookup',
      captionUser: 'How do I renew a permit?',
      voiceInputReadyNonce: 2,
      errorMessage: 'Voice service is temporarily unavailable.',
      commitErrorMessage: 'Could not save this voice turn.',
    });

    expect(screen.getByText('Looking up docs')).toBeInTheDocument();
    expect(screen.getByText('How do I renew a permit?')).toBeInTheDocument();
    expect(screen.getByText('Ready for your follow-up.')).toBeInTheDocument();
    expect(screen.getByText('Voice service is temporarily unavailable.')).toBeInTheDocument();
    expect(screen.getByText('Could not save this voice turn.')).toBeInTheDocument();
  });

  it('renders voice-mode transcript history separately from live input', () => {
    renderVoiceControls({
      connectionState: 'live',
      phase: 'listening',
      captionUser: 'What services help my child?',
      layout: 'full',
      transcriptMessages: [
        { role: 'user', content: 'Tell me about DDS.' },
        { role: 'bot', content: 'DDS supports eligible Californians through Regional Centers.' },
        { role: 'user', content: 'What services help my child?', isStreaming: true },
      ],
    });

    expect(screen.getByText('Live input')).toBeInTheDocument();
    expect(screen.getAllByText('What services help my child?')).toHaveLength(1);
    expect(screen.getByText('DDS supports eligible Californians through Regional Centers.')).toBeInTheDocument();
    expect(screen.getByLabelText('Voice conversation transcript')).toBeInTheDocument();
  });
});
