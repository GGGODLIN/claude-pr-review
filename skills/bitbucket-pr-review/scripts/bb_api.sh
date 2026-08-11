#!/bin/bash
# Bitbucket API helper — handles token loading and authentication
# Usage: bb_api.sh <endpoint>
# Example: bb_api.sh /repositories/your-workspace/your-repo/pullrequests/1234
# Credentials: BITBUCKET_EMAIL (your Atlassian account email) + BITBUCKET_API_TOKEN.
# Both are read env-first, then grepped out of the shell secrets files listed in
# BITBUCKET_SECRETS_FILES (default: ~/.zsh_secrets ~/.zshrc — adjust to your own setup).

SECRETS_FILES="${BITBUCKET_SECRETS_FILES:-$HOME/.zsh_secrets $HOME/.zshrc}"

load_from_secrets() {
  local var="$1" value=''
  for f in $SECRETS_FILES; do
    [ -f "$f" ] || continue
    value=$(grep -m1 "^[[:space:]]*export[[:space:]]*${var}=" "$f" | cut -d'"' -f2)
    [ -n "$value" ] && break
  done
  echo "$value"
}

if [ -z "$BITBUCKET_API_TOKEN" ]; then
  BITBUCKET_API_TOKEN=$(load_from_secrets BITBUCKET_API_TOKEN)
fi

if [ -z "$BITBUCKET_EMAIL" ]; then
  BITBUCKET_EMAIL=$(load_from_secrets BITBUCKET_EMAIL)
fi

if [ -z "$BITBUCKET_API_TOKEN" ]; then
  echo "ERROR: BITBUCKET_API_TOKEN not in env or in: $SECRETS_FILES" >&2
  exit 1
fi

if [ -z "$BITBUCKET_EMAIL" ]; then
  echo "ERROR: BITBUCKET_EMAIL not in env or in: $SECRETS_FILES" >&2
  exit 1
fi

ENDPOINT="$1"
BASE_URL="https://api.bitbucket.org/2.0"

curl -sL -u "${BITBUCKET_EMAIL}:$BITBUCKET_API_TOKEN" "${BASE_URL}${ENDPOINT}"
