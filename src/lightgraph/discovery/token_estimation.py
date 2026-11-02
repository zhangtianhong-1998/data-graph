"""Local, value-free sizing for providers without a matching tokenizer.

This is a heuristic, not the model's tokenizer. English text, JSON punctuation
and non-ASCII text have different costs; four characters per token is not a
valid general rule for Chinese. Provider usage is authoritative after a call.
"""
from __future__ import annotations

from math import ceil


TOKEN_ESTIMATE_METHOD = 'mixed_text_v1'


def estimate_tokens(text: str) -> int:
    """Estimate ASCII runs at 4 chars/token, punctuation at 1, Unicode at 2 B/token.

    JSON delimiters are deliberately conservative. UTF-8 bytes are used only
    for the non-ASCII fallback, never as a one-byte/one-token measurement.
    The result and the configured input window remain explicitly estimates.
    """
    ascii_text = punctuation = unicode_bytes = 0
    for character in text:
        if ord(character) < 128:
            if character.isalnum() or character.isspace():
                ascii_text += 1
            else:
                punctuation += 1
        else:
            unicode_bytes += len(character.encode('utf-8'))
    return ceil(ascii_text / 4 + punctuation + unicode_bytes / 2)


def prompt_token_limit(config) -> int | None:
    """Apply the input cap and reserve completion space in a shared context window."""
    limits = []
    if getattr(config, 'max_prompt_tokens', None) is not None:
        limits.append(config.max_prompt_tokens)
    if getattr(config, 'context_window_tokens', None) is not None:
        limits.append(config.context_window_tokens - config.max_output_tokens)
    return min(limits) if limits else None
