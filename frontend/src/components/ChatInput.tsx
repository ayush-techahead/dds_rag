import React, { useState, type KeyboardEvent } from 'react';
import { Send, Loader2 } from 'lucide-react';
import './ChatInput.css';

interface ChatInputProps {
    onSend: (msg: string) => void;
    disabled?: boolean;
}

export const ChatInput: React.FC<ChatInputProps> = ({ onSend, disabled }) => {
    const [value, setValue] = useState('');

    const handleSend = () => {
        if (value.trim() && !disabled) {
            onSend(value.trim());
            setValue('');
        }
    };

    const onKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            handleSend();
        }
    };

    return (
        <div className="chat-input-container">
            <div className={`chat-input-wrapper ${disabled ? 'disabled' : ''}`}>
                <input
                    type="text"
                    className="chat-input-field"
                    placeholder="Ask something..."
                    value={value}
                    onChange={(e) => setValue(e.target.value)}
                    onKeyDown={onKeyDown}
                    disabled={disabled}
                />
                <button
                    onClick={handleSend}
                    disabled={!value.trim() || disabled}
                    className="chat-send-button"
                >
                    {disabled ? <Loader2 className="spinner-icon" /> : <Send className="icon" />}
                </button>
            </div>
        </div>
    );
};
