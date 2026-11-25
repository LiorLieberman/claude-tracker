#!/usr/bin/env bash
# Adds the tracker hooks to ~/.claude/settings.json, the /done and /ack
# commands, and a launchd agent that keeps the UI server running.
# Running it again replaces what an earlier run added.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
python="$(command -v python3)"
notifier="$(command -v terminal-notifier)" || { echo "Needs terminal-notifier: brew install terminal-notifier" >&2; exit 1; }
hook="$python $here/tracker.py hook"
settings="$HOME/.claude/settings.json"
label="local.claude-tracker"
plist="$HOME/Library/LaunchAgents/$label.plist"

mkdir -p "$HOME/.claude/tracker" "$HOME/.claude/commands"

cp "$settings" "$settings.bak-claude-tracker"
jq --arg cmd "$hook" '
  def ours: any(.hooks[]?; .command == $cmd);
  def add(event; matcher):
    .hooks[event] = ((.hooks[event] // []) | map(select(ours | not)))
      + [{matcher: matcher, hooks: [{type: "command", command: $cmd, timeout: 5}]}];
  add("UserPromptSubmit"; "")
  | add("Stop"; "")
  | add("Notification"; "permission_prompt|elicitation_dialog")
  | add("PreToolUse"; "AskUserQuestion|ExitPlanMode")
  | add("SessionEnd"; "clear")
' "$settings.bak-claude-tracker" > "$settings"

for cmd in done ack; do
  cat > "$HOME/.claude/commands/$cmd.md" <<EOF
---
description: Mark this chat $cmd in claude-tracker
disable-model-invocation: true
---
The claude-tracker hook should have caught /$cmd before it reached you. Reply with only this line: "/$cmd was not recorded, see ~/.claude/tracker/errors.log".
EOF
done

cat > "$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key>
  <array><string>$python</string><string>$here/tracker.py</string><string>serve</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ProcessType</key><string>Background</string>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>$(dirname "$notifier"):/usr/bin:/bin:/usr/sbin:/sbin</string></dict>
  <key>StandardErrorPath</key><string>$HOME/.claude/tracker/server.log</string>
</dict>
</plist>
EOF
launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true  # not loaded on a first install
launchctl bootstrap "gui/$(id -u)" "$plist"

echo "Installed. Settings backup: $settings.bak-claude-tracker"
echo "UI: http://127.0.0.1:7878"
