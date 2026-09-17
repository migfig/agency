from __future__ import annotations

from unittest.mock import Mock, patch

import pytest

from agency.resource_manager.vram_tracker import (
    detect_gpu_vram,
    parse_vram_size,
    resolve_vram_limit,
)
from agency.yaml_engine.parser import SchemaValidationError, load_workflow


class TestParseVramSize:
    def test_gibibytes(self):
        assert parse_vram_size("14GB") == 14 * (1024 ** 3)

    def test_mebibytes(self):
        assert parse_vram_size("8000MB") == 8000 * (1024 ** 2)

    def test_kibibytes(self):
        assert parse_vram_size("1024KB") == 1024 * 1024

    def test_bytes(self):
        assert parse_vram_size("512B") == 512

    def test_case_insensitive(self):
        assert parse_vram_size("14gb") == parse_vram_size("14GB")
        assert parse_vram_size("8Gb") == parse_vram_size("8GB")

    def test_with_whitespace(self):
        assert parse_vram_size(" 14 GB ") == 14 * (1024 ** 3)

    def test_decimal_value(self):
        assert parse_vram_size("1.5GB") == int(1.5 * (1024 ** 3))

    def test_invalid_format_raises(self):
        with pytest.raises(ValueError, match="Invalid VRAM size format"):
            parse_vram_size("abc")

    def test_no_suffix_raises(self):
        with pytest.raises(ValueError, match="Invalid VRAM size format"):
            parse_vram_size("14000")

    def test_unknown_suffix_raises(self):
        with pytest.raises(ValueError, match="Invalid VRAM size format"):
            parse_vram_size("14TB")


class TestDetectGpuVram:
    def test_returns_none_when_pynvml_unavailable(self):
        with patch.dict("sys.modules", {"pynvml": None}):
            result = detect_gpu_vram()
            assert result is None

    def test_returns_total_when_pynvml_available(self):
        mock_info = type("MemoryInfo", (), {"total": 16 * (1024 ** 3)})()
        mock_pynvml = Mock()
        mock_pynvml.nvmlDeviceGetMemoryInfo.return_value = mock_info
        with patch.dict("sys.modules", {"pynvml": mock_pynvml}):
            result = detect_gpu_vram()
            assert result == 16 * (1024 ** 3)

    def test_returns_none_on_pynvml_error(self):
        mock_pynvml = Mock()
        mock_pynvml.nvmlInit.side_effect = Exception("No GPU")
        with patch.dict("sys.modules", {"pynvml": mock_pynvml}):
            result = detect_gpu_vram()
            assert result is None


class TestResolveVramLimit:
    def test_cli_takes_precedence(self):
        result = resolve_vram_limit(cli_value="8GB", yaml_value="14GB")
        assert result.limit_bytes == 8 * (1024 ** 3)
        assert result.source == "CLI"

    def test_yaml_used_when_no_cli(self):
        result = resolve_vram_limit(cli_value=None, yaml_value="14GB")
        assert result.limit_bytes == 14 * (1024 ** 3)
        assert result.source == "YAML config"

    def test_gpu_default_when_neither_set(self):
        with patch("agency.resource_manager.vram_tracker.detect_gpu_vram", return_value=16 * (1024 ** 3)):
            result = resolve_vram_limit(cli_value=None, yaml_value=None)
            expected = int(16 * (1024 ** 3) * 0.8)
            assert result.limit_bytes == expected
            assert result.source == "GPU default"

    def test_none_when_no_config_and_no_gpu(self):
        with patch("agency.resource_manager.vram_tracker.detect_gpu_vram", return_value=None):
            result = resolve_vram_limit(cli_value=None, yaml_value=None)
            assert result.limit_bytes is None
            assert result.source == "none"

    def test_cli_invalid_format_raises(self):
        with pytest.raises(ValueError, match="Invalid VRAM size format"):
            resolve_vram_limit(cli_value="xyz")


class TestSchemaVramValidation:
    def test_valid_vram_limit_passes(self):
        w = load_workflow(
            "name: test\n"
            "nodes:\n"
            "  a: {id: a, type: agent, model: m, prompt_template: p}\n"
            "entry_point: a\n"
            "vram_limit: 14GB"
        )
        assert w.vram_limit == "14GB"

    def test_invalid_vram_format_rejected(self):
        with pytest.raises(SchemaValidationError, match="Invalid VRAM size format"):
            load_workflow(
                "name: test\n"
                "nodes:\n"
                "  a: {id: a, type: agent, model: m, prompt_template: p}\n"
                "entry_point: a\n"
                "vram_limit: abc"
            )


class TestParseVramEdgeCases:
    def test_empty_string_raises(self):
        with pytest.raises(ValueError, match="Invalid VRAM size format"):
            parse_vram_size("")

    def test_zero_value(self):
        assert parse_vram_size("0GB") == 0

    def test_whitespace_only_raises(self):
        with pytest.raises(ValueError, match="Invalid VRAM size format"):
            parse_vram_size("   ")
