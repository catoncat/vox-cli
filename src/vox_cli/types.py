from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class CacheStatus:
    downloaded: bool
    verified: bool
    cache_dir: Path
    revision: str | None
    has_incomplete: bool
    has_weights: bool
    error: str | None = None


@dataclass
class EndpointResult:
    endpoint: str
    snapshot_path: Path
