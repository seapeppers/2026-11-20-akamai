"""Approximate tokenization and prefix-block hashing shared by gateway and replica, so both
agree on which prefixes are cached. ~4 chars per token; blocks are chained hashes."""
from __future__ import annotations

import hashlib

CHARS_PER_TOKEN = 4


def count_tokens(text: str) -> int:
    return max(1, (len(text) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN)


def messages_text(messages: list[dict]) -> str:
    parts = []
    for m in messages:
        content = m.get("content", "")
        if isinstance(content, list):  # OpenAI content parts
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        parts.append(f"<{m.get('role', 'user')}>{content}")
    return "".join(parts)


def prefix_block_hashes(text: str, block_tokens: int = 64) -> list[str]:
    """Chained hashes of complete blocks; block i's hash covers the whole prefix up to i."""
    block_chars = block_tokens * CHARS_PER_TOKEN
    hashes, h = [], hashlib.sha1()
    for i in range(0, len(text) - block_chars + 1, block_chars):
        h.update(text[i:i + block_chars].encode())
        hashes.append(h.copy().hexdigest()[:16])
    return hashes


def cached_prefix_tokens(request_hashes: list[str], cached: set[str], block_tokens: int = 64) -> int:
    """Tokens of the longest cached prefix (chained hashes: stop at first miss)."""
    n = 0
    for bh in request_hashes:
        if bh not in cached:
            break
        n += 1
    return n * block_tokens
