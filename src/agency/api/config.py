from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ServiceConfig:
    """Process-wide service configuration (FR-016).

    ``log_dir`` is the run-history root, shared with the CLI; the service
    verifies it exists and is writable at startup.
    """

    host: str = "127.0.0.1"
    port: int = 8000
    log_dir: Path | str = "runs"

    def resolved_log_dir(self) -> Path:
        return Path(self.log_dir)
