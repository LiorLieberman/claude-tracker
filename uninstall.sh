#!/usr/bin/env bash
# Removes what install.sh added. Leaves the data in ~/.claude/tracker.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
hook="$(command -v python3) $here/tracker.py hook"
settings="$HOME/.claude/settings.json"
label="local.claude-tracker"

cp "$settings" "$settings.bak-claude-tracker"
jq --arg cmd "$hook" '
  .hooks |= (map_values(map(select(any(.hooks[]?; .command == $cmd) | not)))
             | with_entries(select(.value != [])))
  | if .hooks == {} then del(.hooks) else . end
' "$settings.bak-claude-tracker" > "$settings"

rm -f "$HOME/.claude/commands/done.md" "$HOME/.claude/commands/ack.md"
launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true  # already stopped
rm -f "$HOME/Library/LaunchAgents/$label.plist"

echo "Uninstalled. Data left in ~/.claude/tracker"
