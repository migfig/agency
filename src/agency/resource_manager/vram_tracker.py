from __future__ import annotations

import logging
import re
from typing import NamedTuple

logger = logging.getLogger(__name__)

_SIZE_PATTERN = re.compile(
    r"^\s*(\d+(?:\.\d+)?)\s*(b|kb|mb|gb)\s*$", re.IGNORECASE
)

_SUFFIX_MULTIPLIERS = {
    "b": 1,
    "kb": 1024,
    "mb": 1024 ** 2,
    "gb": 1024 ** 3,
}


class VRAMResolutionResult(NamedTuple):
    limit_bytes: int | None
    source: str


def parse_vram_size(value: str) -> int:
    match = _SIZE_PATTERN.match(value)
    if not match:
        raise ValueError(f"Invalid VRAM size format: '{value}' (expected e.g. '14GB', '8000MB')")
    amount = float(match.group(1))
    suffix = match.group(2).lower()
    return int(amount * _SUFFIX_MULTIPLIERS[suffix])


def detect_gpu_vram() -> int | None:
    try:
        import pynvml
    except ImportError:
        logger.warning("pynvml not installed; cannot auto-detect GPU VRAM")
        return None

    try:
        pynvml.nvmlInit()
        handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        info = pynvml.nvmlDeviceGetMemoryInfo(handle)
        pynvml.nvmlShutdown()
        return info.total
    except Exception as exc:  # noqa: BLE001 - optional best-effort dependency
        logger.warning("Failed to query GPU VRAM via pynvml: %s", exc)
        return None


def resolve_vram_limit(
    cli_value: str | None = None,
    yaml_value: str | None = None,
) -> VRAMResolutionResult:
    if cli_value is not None:
        limit = parse_vram_size(cli_value)
        logger.info("VRAM limit set to %s bytes (source: CLI)", limit)
        return VRAMResolutionResult(limit_bytes=limit, source="CLI")

    if yaml_value is not None:
        limit = parse_vram_size(yaml_value)
        logger.info("VRAM limit set to %s bytes (source: YAML config)", limit)
        return VRAMResolutionResult(limit_bytes=limit, source="YAML config")

    gpu_total = detect_gpu_vram()
    if gpu_total is not None:
        default_limit = int(gpu_total * 0.8)
        logger.info(
            "VRAM limit set to %s bytes (source: GPU default, 80%% of %s)",
            default_limit,
            gpu_total,
        )
        return VRAMResolutionResult(limit_bytes=default_limit, source="GPU default")

    logger.warning(
        "No VRAM limit configured and GPU detection unavailable; running without VRAM cap"
    )
    return VRAMResolutionResult(limit_bytes=None, source="none")
