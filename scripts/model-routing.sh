#!/bin/bash
set -u

KEY="${1:-}"
FILE="${2:-$(dirname "$0")/../model-routing.env}"

[ "$KEY" = "GEMINI_FLASH_MODEL" ] || exit 2
[ -f "$FILE" ] || exit 1

COUNT=$(/usr/bin/grep -Ec '^GEMINI_FLASH_MODEL=' "$FILE" 2>/dev/null || true)
[ "$COUNT" -eq 1 ] || exit 1

VALUE=$(/usr/bin/grep -E '^GEMINI_FLASH_MODEL=' "$FILE" | /usr/bin/cut -d= -f2-)
VALUE="${VALUE#"${VALUE%%[![:space:]]*}"}"
VALUE="${VALUE%"${VALUE##*[![:space:]]}"}"
if [[ ${#VALUE} -ge 2 && "$VALUE" == \"*\" ]]; then
  VALUE="${VALUE:1:${#VALUE}-2}"
elif [[ ${#VALUE} -ge 2 && "$VALUE" == \'*\' ]]; then
  VALUE="${VALUE:1:${#VALUE}-2}"
fi

[[ "$VALUE" =~ ^gemini-[0-9]+(\.[0-9]+)*-flash(-(extra-low|medium|low|high))?$ ]] || exit 1
printf '%s\n' "$VALUE"
