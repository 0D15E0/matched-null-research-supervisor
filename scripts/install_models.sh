#!/bin/sh
set -eu

# Local open-weight models only. Cloud aliases are intentionally excluded.
ollama pull qwen3-coder:latest
ollama pull qwen3-coder:30b
printf '%s\n' 'Local Ollama models installed: qwen3-coder:latest and qwen3-coder:30b'