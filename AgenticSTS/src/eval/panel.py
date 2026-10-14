"""Companion panel — a local web page that rewinds the live game to a past room.

Why a panel and not in-game UI: the restore mechanism is already proven and needs
no game-side code, while injecting clickable overlays into the map screen means
reverse-engineering Godot nodes and maintaining that against every game patch
(see the addendum in ``local-tools/SPIKE_FINDINGS_restore.md``). The panel gives
the same rewind over the same data model, today.

It runs a watcher thread (ring every room the player enters) plus a stdlib
HTTP server (no extra dependencies). Rewinding pauses the watcher, restores the
chosen room, and reports what happened.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from src.eval.game_client import EvalGameClient, in_run, run_summary
from src.eval.ring import RoomEntry, RoomRing, watch_rooms
from src.eval.snapshot import RestoreMismatch

PAGE = """<!doctype html>
<html lang="zh"><head><meta charset="utf-8"><title>AgenticSTS rewind</title>
<style>
 body{font:14px/1.5 ui-sans-serif,system-ui,Segoe UI,Roboto,sans-serif;margin:0;background:#12141a;color:#e7e9ee}
 header{padding:14px 18px;background:#1a1d26;border-bottom:1px solid #282c38;display:flex;gap:18px;align-items:baseline;flex-wrap:wrap}
 h1{font-size:16px;margin:0;font-weight:600}
 .muted{color:#98a0b3}
 main{padding:18px;display:grid;grid-template-columns:1fr 380px;gap:18px;align-items:start}
 table{border-collapse:collapse;width:100%;font-size:13px}
 th,td{padding:7px 9px;border-bottom:1px solid #262a35;text-align:left;white-space:nowrap}
 th{color:#98a0b3;font-weight:500;position:sticky;top:0;background:#12141a}
 tr:hover td{background:#1b1f2a}
 tr.current td{background:#16301f}
 button{background:#2b6cb0;color:#fff;border:0;border-radius:6px;padding:5px 11px;cursor:pointer;font-size:12px}
 button:hover{background:#3182ce} button:disabled{background:#39404f;cursor:default}
 #log{background:#0d0f14;border:1px solid #262a35;border-radius:8px;padding:10px;height:420px;overflow:auto;font:12px/1.5 ui-monospace,Consolas,monospace;white-space:pre-wrap}
 .ok{color:#68d391}.err{color:#fc8181}.warn{color:#f6e05e}
</style></head><body>
<header>
  <h1>AgenticSTS · 回溯面板</h1>
  <span class="muted" id="live">连接中…</span>
  <span class="muted" id="ringinfo"></span>
</header>
<main>
  <div><table>
    <thead><tr><th>层</th><th>房间</th><th>快照时 HP</th><th>金币</th><th>牌组</th><th>抓取时间</th><th></th></tr></thead>
    <tbody id="rows"></tbody>
  </table></div>
  <div><div id="log"></div></div>
</main>
<script>
const logEl = document.getElementById('log');
function log(msg, cls){ const t=new Date().toLocaleTimeString();
  logEl.innerHTML += `<span class="${cls||''}">[${t}] ${msg}</span>\\n`; logEl.scrollTop = logEl.scrollHeight; }
async function refresh(){
  try{
    const r = await fetch('/api/state'); const d = await r.json();
    document.getElementById('live').textContent = d.live;
    document.getElementById('ringinfo').textContent = `已记录 ${d.rooms.length} 个房间 · 种子 ${d.seed||'?'}`;
    const cur = d.current_key;
    document.getElementById('rows').innerHTML = d.rooms.map(e => `
      <tr class="${e.key===cur?'current':''}">
        <td>${e.act}-${e.floor}</td><td>${e.screen}${e.node&&e.node.node_type?' · '+e.node.node_type:''}</td>
        <td>${e.hp??'?'}/${e.max_hp??'?'}</td><td>${e.gold??'?'}</td><td>${e.deck_size}</td>
        <td class="muted">${(e.captured_at||'').replace('T',' ').replace('+00:00','Z')}</td>
        <td><button data-label="${e.label}" ${d.busy?'disabled':''}>回到这里</button></td>
      </tr>`).join('');
    document.querySelectorAll('button[data-label]').forEach(b => b.onclick = () => rewind(b.dataset.label));
  }catch(e){ document.getElementById('live').textContent = '无法连接面板'; }
}
async function rewind(label){
  log(`回溯到 ${label} …`);
  const r = await fetch('/api/rewind', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({label})});
  const d = await r.json();
  log(d.message, d.ok ? 'ok' : 'err');
  refresh();
}
refresh(); setInterval(refresh, 2000);
</script></body></html>
"""


class Panel:
    """Holds the live state the page polls, and serialises restores."""

    def __init__(self, ring: RoomRing, *, port: int = 8760) -> None:
        self.ring = ring
        self.port = port
        self.lock = threading.Lock()
        self.live: dict[str, Any] = {}
        self.busy = False
        self.log: list[str] = []
        self._client: EvalGameClient | None = None

    # ── shared client (single connection, used by watcher and rewinder) ──

    def client(self) -> EvalGameClient:
        if self._client is None:
            self._client = EvalGameClient()
        return self._client

    # ── page data ──────────────────────────────────────────────────────

    def snapshot_payload(self) -> dict[str, Any]:
        state = self.live or {}
        run = state.get("run") or {}
        return {
            "live": run_summary(state) if state else "no state yet",
            "seed": state.get("run_id"),
            "current_key": None if not in_run(state) else f"a{state.get('act') or 1}-f{run.get('floor')}",
            "busy": self.busy,
            "rooms": self.ring.as_dicts(),
        }

    def note(self, message: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self.log.append(f"[{stamp}] {message}")
        if len(self.log) > 300:
            del self.log[:-300]

    # ── actions ────────────────────────────────────────────────────────

    def rewind(self, label: str) -> dict[str, Any]:
        with self.lock:
            self.busy = True
            entry: RoomEntry | None = self.ring.find(label)
            if entry is None:
                self.busy = False
                return {"ok": False, "message": f"没有这个快照: {label}"}
            try:
                target, previous, state = self.ring.rewind(self.client(), label, verify=True)
            except RestoreMismatch as exc:
                self.busy = False
                return {"ok": False, "message": f"恢复不一致,已中止: {exc}"}
            except Exception as exc:  # noqa: BLE001 - surfaced to the page
                self.busy = False
                return {"ok": False, "message": f"回溯失败: {type(exc).__name__}: {exc}"}
            finally:
                self.busy = False
            where = run_summary(state)
            self.live = state
            message = (
                f"已回到 a{target.act}-f{target.floor} ({target.screen});现在:{where}"
                + (f"  [原来在 {previous.key}]" if previous and previous.key != target.key else "")
            )
            self.note(message)
            return {"ok": True, "message": message, "state": where}


def _handler(panel: Panel) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: object) -> None:  # keep the console calm
            return

        def _send(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            if self.path in ("/", "/index.html"):
                self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            elif self.path == "/api/state":
                payload = json.dumps(panel.snapshot_payload(), ensure_ascii=False)
                self._send(200, payload.encode("utf-8"), "application/json; charset=utf-8")
            elif self.path == "/api/log":
                self._send(200, json.dumps(panel.log).encode("utf-8"), "application/json")
            else:
                self._send(404, b"not found", "text/plain")

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/api/rewind":
                self._send(404, b"not found", "text/plain")
                return
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                self._send(400, b'{"ok":false,"message":"bad json"}', "application/json")
                return
            result = panel.rewind(str(body.get("label", "")))
            self._send(
                200 if result.get("ok") else 409,
                json.dumps(result, ensure_ascii=False).encode("utf-8"),
                "application/json; charset=utf-8",
            )

    return Handler


def serve(
    panel: Panel,
    *,
    watch: bool = True,
    interval: float = 1.0,
    open_browser: bool = False,
) -> None:
    """Start the watcher thread and block on the HTTP server."""
    stop = threading.Event()

    def on_state(state: dict[str, Any]) -> None:
        panel.live = state

    def on_capture(entry: RoomEntry) -> None:
        panel.note(f"记录房间 a{entry.act}-f{entry.floor} ({entry.screen})")

    if watch:
        thread = threading.Thread(
            target=watch_rooms,
            kwargs={
                "client": panel.client(),
                "ring": panel.ring,
                "interval": interval,
                "stop": stop.is_set,
                "on_capture": on_capture,
                "on_state": on_state,
                "pause": panel.lock,
            },
            daemon=True,
            name="room-watcher",
        )
        thread.start()

    server = ThreadingHTTPServer(("127.0.0.1", panel.port), _handler(panel))
    server.daemon_threads = True
    url = f"http://127.0.0.1:{panel.port}/"
    panel.note(f"面板已启动: {url}")
    if open_browser:
        import webbrowser

        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        panel.note("面板停止")
    finally:
        stop.set()
        server.server_close()
