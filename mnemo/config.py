"""Configuration loading.

Everything that is deployment-specific lives here: which layers exist,
how many entries each layer keeps hot, where the database file is.
Nothing in this repository hard-codes a path, a domain or a token.
"""

import json
import os
from dataclasses import dataclass, field
from typing import Dict, List

DEFAULT_LAYERS: List[str] = ["core", "daily", "memo", "diary", "note"]

# Per-layer cap on how many entries stay "hot" for a given tag.
# When the cap is exceeded, the oldest entries are demoted (active_flag = 0)
# rather than deleted. See docs/design.md, "Cold sinking".
DEFAULT_HOT_LIMITS: Dict[str, int] = {"core": 2}


@dataclass
class Config:
    db_path: str = "./mnemo.db"
    host: str = "127.0.0.1"
    port: int = 3456
    token: str = ""
    layers: List[str] = field(default_factory=lambda: list(DEFAULT_LAYERS))
    hot_limits: Dict[str, int] = field(default_factory=lambda: dict(DEFAULT_HOT_LIMITS))
    default_layer: str = "daily"
    read_limit: int = 20
    search_limit: int = 10

    @classmethod
    def load(cls, path: str = "config.json") -> "Config":
        """Load config.json if present, then let environment variables win.

        Environment overrides: MNEMO_DB, MNEMO_HOST, MNEMO_PORT, MNEMO_TOKEN.
        """
        data = {}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)

        cfg = cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

        cfg.db_path = os.environ.get("MNEMO_DB", cfg.db_path)
        cfg.host = os.environ.get("MNEMO_HOST", cfg.host)
        cfg.port = int(os.environ.get("MNEMO_PORT", cfg.port))
        cfg.token = os.environ.get("MNEMO_TOKEN", cfg.token)

        if not cfg.token:
            raise SystemExit(
                "No token configured. Set MNEMO_TOKEN in the environment, or "
                '"token" in config.json. The server refuses to start without one.'
            )
        return cfg
