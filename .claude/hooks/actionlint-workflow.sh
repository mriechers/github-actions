#!/usr/bin/env bash
# PostToolUse(Write|Edit): run actionlint on an edited GitHub Actions workflow.
# This repo ships workflow YAML as its product and has no test suite, so a typo
# would otherwise only surface as a failed run in a consumer repo.
set -uo pipefail

file=$(jq -r '.tool_input.file_path // .tool_response.filePath // empty')

case "$file" in
  */.github/workflows/*.yml | */.github/workflows/*.yaml) ;;
  *) exit 0 ;;
esac

command -v actionlint >/dev/null 2>&1 || exit 0

if out=$(actionlint "$file" 2>&1); then
  exit 0
fi

jq -nc --arg out "$out" \
  '{decision:"block",reason:("actionlint found problems in this workflow:\n"+$out)}'
