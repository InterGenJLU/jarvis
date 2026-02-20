"""
DocumentBuffer — In-memory document context for LLM augmentation.

Shared between jarvis_console.py and jarvis_web.py.
Loaded content is injected as <document> XML tags into LLM queries.
"""

from core.context_window import estimate_tokens, TOKEN_RATIO


class DocumentBuffer:
    """In-memory document context that persists until cleared."""

    def __init__(self, max_tokens: int = 4000):
        self.content: str = ""
        self.source: str = ""       # "paste", "file:name.py", "clipboard"
        self.token_estimate: int = 0
        self.max_tokens = max_tokens

    def load(self, text: str, source: str = "paste"):
        self.content = text
        self.source = source
        self.token_estimate = estimate_tokens(text)
        self.truncate_to_budget()

    def append(self, text: str, source: str = "paste"):
        self.content = self.content + "\n\n" + text if self.content else text
        self.source = f"{self.source} + {source}" if self.source else source
        self.token_estimate = estimate_tokens(self.content)
        self.truncate_to_budget()

    def clear(self):
        old_source = self.source
        old_tokens = self.token_estimate
        self.content = ""
        self.source = ""
        self.token_estimate = 0
        return old_source, old_tokens

    @property
    def active(self) -> bool:
        return bool(self.content)

    def build_augmented_message(self, user_query: str) -> str:
        if not self.content:
            return user_query
        return f"<document>\n{self.content}\n</document>\n\n{user_query}"

    def truncate_to_budget(self) -> bool:
        if self.token_estimate <= self.max_tokens:
            return False
        words = self.content.split()
        target_words = int(self.max_tokens / TOKEN_RATIO)
        self.content = " ".join(words[:target_words])
        self.token_estimate = estimate_tokens(self.content)
        if "(truncated)" not in self.source:
            self.source += " (truncated)"
        return True


# Binary file extensions rejected by /file command and drag/drop
BINARY_EXTENSIONS = frozenset({
    '.exe', '.bin', '.so', '.dll', '.dylib', '.o', '.a',
    '.png', '.jpg', '.jpeg', '.gif', '.bmp', '.ico', '.webp', '.tiff',
    '.mp3', '.mp4', '.wav', '.flac', '.ogg', '.avi', '.mkv', '.mov', '.webm',
    '.zip', '.tar', '.gz', '.bz2', '.xz', '.7z', '.rar', '.zst',
    '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx',
    '.gguf', '.npy', '.npz', '.pt', '.pth', '.onnx', '.safetensors',
    '.db', '.sqlite', '.sqlite3',
    '.pyc', '.class', '.wasm',
})
