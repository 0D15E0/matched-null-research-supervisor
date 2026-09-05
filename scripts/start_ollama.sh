#!/bin/sh
set -eu

endpoint="${OLLAMA_HOST:-http://127.0.0.1:11434}"
case "$endpoint" in
  http://127.0.0.1:11434|http://127.0.0.1:11434/)
    ;;
  *)
    printf '%s\n' 'Refusing non-loopback OLLAMA_HOST' >&2
    exit 2
    ;;
esac

if curl -fsS "$endpoint/api/tags" >/dev/null 2>&1; then
  printf '%s\n' 'Ollama is already running on loopback.'
  exit 0
fi

exec ollama serve