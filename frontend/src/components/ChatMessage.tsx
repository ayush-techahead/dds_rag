import React, { type AnchorHTMLAttributes } from 'react';
import { User, Bot, Sparkles, Mic } from 'lucide-react';
import ReactMarkdown, { type Components } from 'react-markdown';
import remarkGfm from 'remark-gfm';
import './ChatMessage.css';

function isSafeHref(href: string | undefined): boolean {
    if (!href?.trim()) return false;
    const t = href.trim().toLowerCase();
    if (t.startsWith('javascript:') || t.startsWith('vbscript:') || t.startsWith('data:')) return false;
    return (
        t.startsWith('http://') ||
        t.startsWith('https://') ||
        t.startsWith('mailto:') ||
        t.startsWith('#')
    );
}

function MarkdownLink({ href, children, className }: AnchorHTMLAttributes<HTMLAnchorElement>) {
    if (!isSafeHref(href)) {
        return <span className={className}>{children}</span>;
    }
    const openInNewTab =
        href!.startsWith('http://') || href!.startsWith('https://');
    return (
        <a
            href={href}
            className={className}
            {...(openInNewTab ? { target: '_blank', rel: 'noopener noreferrer' } : {})}
        >
            {children}
        </a>
    );
}

const botMarkdownComponents: Components = {
    a: MarkdownLink,
};

interface ChatMessageProps {
    role: 'user' | 'bot';
    content: string;
    /** From API — show mic affordance for voice-committed turns */
    source?: 'text' | 'voice';
    isStreaming?: boolean;
    loadingText?: string | null;
    options?: string[];
    onOptionClick?: (opt: string) => void;
}

export const ChatMessage: React.FC<ChatMessageProps> = ({
    role,
    content,
    source,
    isStreaming,
    loadingText,
    options,
    onOptionClick
}) => {
    const isUser = role === 'user';

    return (
        <div className={`message-row ${isUser ? 'message-row-user' : 'message-row-bot'}`}>
            <div className={`message-container ${isUser ? 'message-container-user' : 'message-container-bot'}`}>
                {/* Avatar */}
                <div className={`avatar ${isUser ? 'avatar-user' : 'avatar-bot'}`}>
                    {isUser ? <User className="avatar-icon" /> : <Bot className="avatar-icon-bot" />}
                </div>

                {/* Bubble & content */}
                <div className={`message-content-wrapper ${isUser ? 'items-end' : 'items-start'}`}>
                    <div
                        className={`message-bubble ${isUser ? 'bubble-user' : 'bubble-bot'}${source === 'voice' ? ' message-bubble-voice' : ''}`}
                    >
                        {source === 'voice' && (
                            <span
                                className="message-voice-badge"
                                title="Voice"
                                aria-label="Voice message"
                            >
                                <Mic size={14} strokeWidth={2} aria-hidden />
                            </span>
                        )}
                        {!content && role === 'bot' && isStreaming ? (
                            <div className="analyzing-loader">
                                <Sparkles className="sparkle-icon" />
                                <span>{loadingText || 'Analyzing...'}</span>
                            </div>
                        ) : (
                            <>
                                {role === 'bot' ? (
                                    <div className="message-markdown">
                                        <ReactMarkdown
                                            remarkPlugins={[remarkGfm]}
                                            components={botMarkdownComponents}
                                        >
                                            {content}
                                        </ReactMarkdown>
                                    </div>
                                ) : (
                                    <>
                                        {content}
                                        {isStreaming && role === 'user' && <span className="cursor-blink" />}
                                    </>
                                )}
                                {isStreaming && role === 'bot' && (
                                    <span className="cursor-blink" />
                                )}
                            </>
                        )}
                    </div>

                    {/* Options */}
                    {role === 'bot' && options && options.length > 0 && (
                        <div className="options-container">
                            {options.map((opt, idx) => (
                                <button
                                    key={idx}
                                    onClick={() => onOptionClick?.(opt)}
                                    className="option-button"
                                >
                                    {opt}
                                </button>
                            ))}
                        </div>
                    )}
                </div>
            </div>
        </div>
    );
};
