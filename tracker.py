#!/usr/bin/env python3
"""Keeps a list of Claude Code chats so none get forgotten.

tracker.py hook   Claude Code hooks call this with the event JSON on stdin.
tracker.py serve  Serves the UI on http://127.0.0.1:7878, indexes transcripts
                  for search, and sends a macOS notification for idle chats.
tracker.py scan   Indexes transcripts once and exits.
"""

import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import traceback
import uuid
from contextlib import closing
from pathlib import Path

PROJECTS = Path.home() / ".claude" / "projects"
DATA = Path.home() / ".claude" / "tracker"
DB_PATH = DATA / "tracker.db"
ERRORS = DATA / "errors.log"
UI = Path(__file__).with_name("ui.html")

PORT = 7878
PING_AFTER = 20 * 60  # your turn and nothing happened for this long
STALLED_AFTER = 60 * 60  # Claude's turn but the transcript is quiet: stuck, crashed or interrupted
SCAN_EVERY = 60
BACKFILL_ACTIVE = 3 * 24 * 3600  # chats the first scan finds stay active if touched this recently

MARK = re.compile(r"\s*/(done|ack)\b")
SKIP_TEXT = ("<", "[Request interrupted")  # IDE context, command output and interrupt markers

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY,
  cwd TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL DEFAULT '',
  first_prompt TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'active',  -- active, acked or done
  turn TEXT NOT NULL DEFAULT 'you',       -- you or claude
  last_activity REAL NOT NULL DEFAULT 0,  -- last hook event or transcript write
  pinged_at REAL NOT NULL DEFAULT 0,
  scanned_bytes INTEGER NOT NULL DEFAULT 0,
  name TEXT NOT NULL DEFAULT '',          -- set by a rename in the UI or in Claude Code
  claude_name TEXT NOT NULL DEFAULT ''    -- last rename seen in the transcript
);
CREATE VIRTUAL TABLE IF NOT EXISTS messages USING fts5(
  session_id UNINDEXED, text, tokenize = 'trigram'
);
"""


def connect():
    DATA.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode = WAL")
    db.executescript(SCHEMA)
    return db


def log_error():
    DATA.mkdir(parents=True, exist_ok=True)
    with ERRORS.open("a") as f:
        f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + traceback.format_exc() + "\n")


def hook(event):
    if os.environ.get("CLAUDE_CODE_ENTRYPOINT", "").startswith("sdk"):
        return  # headless `claude -p` and SDK runs aren't chats
    name = event["hook_event_name"]
    if name == "PreToolUse" and event.get("agent_id"):
        return  # a subagent's tool call doesn't hand the turn to you
    sid, now = event["session_id"], time.time()
    db = connect()
    db.execute(
        "INSERT OR IGNORE INTO sessions (id, cwd, last_activity) VALUES (?, ?, ?)",
        (sid, event.get("cwd", ""), now),
    )
    if name == "UserPromptSubmit":
        prompt = event.get("prompt", "")
        mark = MARK.match(prompt)
        if mark:
            status = "done" if mark[1] == "done" else "acked"
            db.execute("UPDATE sessions SET status = ? WHERE id = ?", (status, sid))
            print(json.dumps({
                "decision": "block",
                "reason": f"claude-tracker: marked {status}",
                "hookSpecificOutput": {"hookEventName": name, "suppressOriginalPrompt": True},
            }))
            return
        db.execute(
            "UPDATE sessions SET status = 'active', turn = 'claude', last_activity = ?,"
            " first_prompt = CASE first_prompt WHEN '' THEN ? ELSE first_prompt END"
            " WHERE id = ?",
            (now, prompt[:300], sid),
        )
    elif name in ("Stop", "Notification", "PreToolUse"):
        db.execute("UPDATE sessions SET turn = 'you', last_activity = ? WHERE id = ?", (now, sid))
    elif name == "SessionEnd":  # settings.json only sends reason "clear"
        db.execute("UPDATE sessions SET status = 'done' WHERE id = ?", (sid,))


def scan(db):
    now = time.time()
    known = dict(db.execute("SELECT id, scanned_bytes FROM sessions").fetchall())
    for path in PROJECTS.glob("*/*.jsonl"):  # subagent transcripts sit one level deeper
        try:
            st = path.stat()
        except FileNotFoundError:
            continue  # deleted since the glob
        sid = path.stem
        start = known.get(sid, 0)
        if st.st_size != start:
            # A file smaller than what we read was rewritten, so read it again from the top.
            start = start if st.st_size > start else 0
            index(db, sid, path, start, st.st_mtime, sid not in known, now)


def index(db, sid, path, start, mtime, new, now):
    with path.open("rb") as f:
        f.seek(start)
        data = f.read()
    end = data.rfind(b"\n") + 1  # a half-written last line waits for the next scan
    if not end:
        return
    title = claude_name = first_prompt = cwd = None
    headless = False
    texts = []
    for line in data[:end].splitlines():
        if not any(k in line for k in (b'"user"', b'"assistant"', b'"ai-title"', b'"custom-title"')):
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            log_error()
            continue
        kind = e.get("type")
        if kind == "ai-title":
            title = e.get("aiTitle") or title
        elif kind == "custom-title":
            claude_name = e.get("customTitle") or claude_name
        elif kind in ("user", "assistant") and not e.get("isSidechain") and not e.get("isMeta"):
            cwd = cwd or e.get("cwd")
            headless = headless or (e.get("entrypoint") or "").startswith("sdk")
            text = message_text(e)
            if text:
                texts.append((sid, text))
                if kind == "user" and first_prompt is None:
                    first_prompt = text[:300]

    db.execute("BEGIN")
    try:
        if start == 0:
            db.execute("DELETE FROM messages WHERE session_id = ?", (sid,))
        db.executemany("INSERT INTO messages (session_id, text) VALUES (?, ?)", texts)
        if new:
            # No hook has seen this chat: it predates the install or is headless.
            # Marking it pinged means it pings only after it moves again.
            recent = not headless and now - mtime < BACKFILL_ACTIVE
            db.execute(
                "INSERT OR IGNORE INTO sessions (id, status, pinged_at) VALUES (?, ?, ?)",
                (sid, "active" if recent else "done", now),
            )
        # Claude Code writes its custom title again on some saves, so only a
        # changed one counts as a new rename and replaces a UI rename.
        db.execute(
            "UPDATE sessions SET scanned_bytes = :end, last_activity = MAX(last_activity, :mtime),"
            " cwd = CASE cwd WHEN '' THEN :cwd ELSE cwd END,"
            " title = COALESCE(:title, title),"
            " first_prompt = CASE first_prompt WHEN '' THEN :first_prompt ELSE first_prompt END,"
            " name = CASE WHEN :claude_name != claude_name THEN :claude_name ELSE name END,"
            " claude_name = COALESCE(:claude_name, claude_name)"
            " WHERE id = :id",
            {"end": start + end, "mtime": mtime, "cwd": cwd or "", "title": title,
             "first_prompt": first_prompt or "", "claude_name": claude_name, "id": sid},
        )
        db.execute("COMMIT")
    except BaseException:
        db.execute("ROLLBACK")
        raise


def message_text(e):
    content = (e.get("message") or {}).get("content")
    if isinstance(content, str):
        parts = [content]
    elif isinstance(content, list):
        parts = [c.get("text") or "" for c in content if isinstance(c, dict) and c.get("type") == "text"]
    else:
        return ""
    parts = (p.strip() for p in parts)
    return "\n".join(p for p in parts if p and not p.startswith(SKIP_TEXT))


def ping(db, now):
    rows = db.execute(
        "SELECT id, name, title, first_prompt FROM sessions"
        " WHERE status = 'active' AND pinged_at < last_activity"
        " AND last_activity <= CASE turn WHEN 'you' THEN ? ELSE ? END",
        (now - PING_AFTER, now - STALLED_AFTER),
    ).fetchall()
    if not rows:
        return
    names = [r["name"] or r["title"] or r["first_prompt"] or r["id"] for r in rows]
    title = f"{len(rows)} Claude chat{'s' if len(rows) > 1 else ''} waiting on you"
    body = "\n".join(names[:3]) + (f"\n+{len(names) - 3} more" if len(names) > 3 else "")
    # Clicking opens the chat itself when there's one, or the UI when there are several.
    url = vscode_url(rows[0]["id"]) if len(rows) == 1 else f"http://127.0.0.1:{PORT}/"
    subprocess.run(
        ["terminal-notifier", "-title", title, "-message", body, "-open", url,
         "-sound", "Glass", "-group", "claude-tracker"],
        check=True, timeout=10,
    )
    db.executemany("UPDATE sessions SET pinged_at = ? WHERE id = ?", [(now, r["id"]) for r in rows])


def watch():
    db = connect()
    while True:
        try:
            scan(db)
            ping(db, time.time())
        except Exception:
            log_error()
        time.sleep(SCAN_EVERY)


def list_sessions(db, query):
    now = time.time()
    words = query.split()
    snippets = {}
    if words:
        ids = None
        for w in words:
            like = f"%{w}%"
            # First matching message per chat, for the snippet.
            hits = dict(db.execute(
                "SELECT session_id, text FROM messages WHERE rowid IN"
                " (SELECT min(rowid) FROM messages WHERE text LIKE ? GROUP BY session_id)",
                (like,),
            ).fetchall())
            for sid, text in hits.items():
                snippets.setdefault(sid, snippet(text, w))
            found = set(hits) | {r[0] for r in db.execute(
                "SELECT id FROM sessions"
                " WHERE name LIKE ?1 OR title LIKE ?1 OR first_prompt LIKE ?1 OR cwd LIKE ?1",
                (like,),
            )}
            ids = found if ids is None else ids & found
        rows = db.execute(
            f"SELECT * FROM sessions WHERE id IN ({','.join('?' * len(ids))})"
            " ORDER BY last_activity DESC LIMIT 100",
            tuple(ids),
        ).fetchall() if ids else []
    else:
        rows = db.execute("SELECT * FROM sessions WHERE status != 'done'").fetchall()
        rows += db.execute(
            "SELECT * FROM sessions WHERE status = 'done' ORDER BY last_activity DESC LIMIT 30"
        ).fetchall()

    out = []
    for r in rows:
        idle = now - r["last_activity"]
        limit = PING_AFTER if r["turn"] == "you" else STALLED_AFTER
        active = r["status"] == "active"
        out.append({
            "id": r["id"],
            "title": r["name"] or r["title"] or r["first_prompt"] or "(no prompt yet)",
            "cwd": r["cwd"],
            "project": os.path.basename(r["cwd"]) or "?",
            "status": r["status"],
            "turn": r["turn"],
            "idle": idle,
            "needs_you": active and (r["turn"] == "you" or idle >= STALLED_AFTER),
            "overdue": active and idle >= limit,
            "snippet": snippets.get(r["id"], ""),
        })
    out.sort(key=lambda s: -s["idle"] if s["needs_you"] else s["idle"])
    errors = ERRORS.exists() and ERRORS.stat().st_size > 0
    return {"sessions": out, "errors": errors}


def snippet(text, word):
    i = text.lower().find(word.lower())
    start = max(0, i - 80)
    cut = " ".join(text[start:i + len(word) + 120].split())
    return ("…" if start else "") + cut + ("…" if i + len(word) + 120 < len(text) else "")


def set_status(db, sid, status):
    if status == "active":
        # Reopening restarts the clock, so it pings again if left alone.
        db.execute(
            "UPDATE sessions SET status = 'active', turn = 'you', last_activity = ? WHERE id = ?",
            (time.time(), sid),
        )
    else:
        db.execute("UPDATE sessions SET status = ? WHERE id = ?", (status, sid))


def vscode_url(sid):
    # The Claude Code extension switches to the chat's tab, or opens the chat in a new one.
    return f"vscode://anthropic.claude-code/open?session={uuid.UUID(sid)}"


def serve():
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import parse_qs, urlparse

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if not self.local():
                return
            url = urlparse(self.path)
            if url.path == "/":
                self.reply(200, "text/html; charset=utf-8", UI.read_bytes())
            elif url.path == "/api/sessions":
                query = parse_qs(url.query).get("q", [""])[0]
                with closing(connect()) as db:
                    sessions = list_sessions(db, query)
                self.reply(200, "application/json", json.dumps(sessions).encode())
            else:
                self.send_error(404)

        def do_POST(self):
            if not self.local():
                return
            # Browsers preflight cross-site JSON posts, and this server never approves one.
            if self.headers.get("Content-Type") != "application/json":
                return self.send_error(403)
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            if self.path == "/api/status" and body.get("status") in ("active", "acked", "done"):
                with closing(connect()) as db:
                    set_status(db, body.get("id"), body["status"])
            elif self.path == "/api/open":
                subprocess.run(["open", vscode_url(body.get("id"))], check=True, timeout=10)
            elif self.path == "/api/rename":
                with closing(connect()) as db:
                    db.execute(
                        "UPDATE sessions SET name = ? WHERE id = ?",
                        ((body.get("name") or "").strip(), body.get("id")),
                    )
            else:
                return self.send_error(400)
            self.reply(204, "text/plain", b"")

        def local(self):
            # Blocks DNS rebinding, where another site points its own name at 127.0.0.1.
            if self.headers.get("Host") in (f"127.0.0.1:{PORT}", f"localhost:{PORT}"):
                return True
            self.send_error(403)
            return False

        def reply(self, code, ctype, body):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    watch()  # if this ever raises, the process exits and launchd restarts it


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "hook":
        try:
            hook(json.load(sys.stdin))
        except Exception:
            log_error()  # a tracker bug must not block a chat; the UI shows that errors exist
    elif cmd == "serve":
        serve()
    elif cmd == "scan":
        scan(connect())
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
