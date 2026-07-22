from unittest.mock import patch, MagicMock

import pytest

import ollama


SAMPLE_OLLAMA_LIST_OUTPUT = """NAME                        ID              SIZE      MODIFIED
qwen3-coder:30b             06c1097efce0    18 GB     3 months ago
qwen2.5-coder:14b           dae161e27b0e    4.7 GB    6 weeks ago
"""


def test_list_ollama_models_parses_output():
    mock_result = MagicMock(returncode=0, stdout=SAMPLE_OLLAMA_LIST_OUTPUT)
    with patch("subprocess.run", return_value=mock_result) as mock_run:
        models = ollama.list_ollama_models()

    assert models == ["qwen3-coder:30b", "qwen2.5-coder:14b"]
    mock_run.assert_called_once()
    assert mock_run.call_args[0][0] == ["ollama", "list"]


def test_list_ollama_models_empty_when_none_pulled():
    header_only = "NAME                        ID              SIZE      MODIFIED\n"
    mock_result = MagicMock(returncode=0, stdout=header_only)
    with patch("subprocess.run", return_value=mock_result):
        models = ollama.list_ollama_models()

    assert models == []


def test_list_ollama_models_raises_when_ollama_not_on_path():
    with patch("subprocess.run", side_effect=FileNotFoundError()):
        with pytest.raises(ollama.OllamaUnavailableError):
            ollama.list_ollama_models()


def test_list_ollama_models_raises_when_command_fails():
    mock_result = MagicMock(returncode=1, stdout="", stderr="connection refused")
    with patch("subprocess.run", return_value=mock_result):
        with pytest.raises(ollama.OllamaUnavailableError):
            ollama.list_ollama_models()
