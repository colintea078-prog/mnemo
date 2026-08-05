"""mnemo — layered long-term memory for LLM agents."""

from .config import Config
from .memory import MemoryStore

__version__ = "1.0.0"
__all__ = ["Config", "MemoryStore"]
