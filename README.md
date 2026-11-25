# claude-tracker

Lists your Claude Code chats and pings you about the ones you left hanging.

Install with `./install.sh`, remove with `./uninstall.sh`. Needs `python3`, `jq` and `terminal-notifier`.

In any chat, type `/done` when you're finished with it, or `/ack` when you don't need anything right now. The hook catches both before they reach Claude, so they cost no tokens. The UI has the same buttons. Sending a new message puts the chat back to active. Click a chat's title to jump to its VS Code tab, or to open it in a new tab if it isn't open.

Rename in the UI changes the name only in the tracker. A rename in VS Code ("Rename Session Tab") shows up in the tracker within a minute and replaces a UI rename.

A macOS notification goes out when an active chat has been your turn for 20 minutes, or when it's Claude's turn but the transcript has been quiet for an hour (stuck, crashed or interrupted). You get one notification per quiet stretch. Clicking it opens the chat, or the UI if several chats are waiting.

`/clear` marks the cleared chat done. Headless `claude -p` runs are ignored.

Data lives in `~/.claude/tracker`: `tracker.db`, `errors.log`, and `server.log` from launchd.

Licensed under the Apache License 2.0. See LICENSE.
