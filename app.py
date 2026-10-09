#!/usr/bin/env python3
"""Pickup Sniper control panel. Run `python3 app.py` and it opens in your web browser.

Uses only the Python standard library; it starts sniper.py for you and shows its output.
"""
import json
import os
import signal
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
SNIPER = HERE / "sniper.py"
PORT = int(os.environ.get("PICKUP_SNIPER_PORT", "8765"))
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


class Runner:
    """Runs one child process at a time (setup, login or run) and collects its output."""

    def __init__(self):
        self.lock = threading.Lock()
        self.proc = None
        self.mode = None
        self.log = []
        self.waiting = False

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self, mode, cmd):
        with self.lock:
            if self.running():
                return False
            self.mode = mode
            self.waiting = False
            self.log.append(f"\n=== {mode} ===\n")
            env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
            kwargs = {}
            if os.name == "nt":
                kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            self.proc = subprocess.Popen(
                cmd, cwd=HERE, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, **kwargs,
            )
            threading.Thread(target=self._pump, args=(self.proc,), daemon=True).start()
            return True

    def _pump(self, proc):
        while True:
            chunk = proc.stdout.read1(4096) if hasattr(proc.stdout, "read1") else proc.stdout.read(1)
            if not chunk:
                break
            text = chunk.decode("utf-8", "replace").replace("\a", "")
            with self.lock:
                self.log.append(text)
                if "press Enter" in text:
                    self.waiting = True
                del self.log[:-2000]
        proc.wait()
        with self.lock:
            self.waiting = False
            self.log.append(f"\n[finished: {self.mode}]\n")

    def enter(self):
        with self.lock:
            if self.running():
                try:
                    self.proc.stdin.write(b"\n")
                    self.proc.stdin.flush()
                except OSError:
                    pass
                self.waiting = False

    def stop(self):
        with self.lock:
            if not self.running():
                return
            if os.name == "nt":
                self.proc.terminate()
            else:
                # Like Ctrl+C: if slots are already in the cart, the browser stays open for checkout.
                self.proc.send_signal(signal.SIGINT)

    def status(self, since):
        with self.lock:
            text = "".join(self.log)
            return {
                "running": self.running(),
                "mode": self.mode if self.running() else None,
                "waiting": self.waiting,
                "log": text[since:] if since <= len(text) else text,
                "size": len(text),
            }


runner = Runner()


VENV = HERE / ".venv"
VENV_PY = VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
READY = VENV / ".ready"


def python():
    """The Python that has Playwright: our private .venv once set up, otherwise this one."""
    return str(VENV_PY) if READY.exists() else sys.executable


def deps_ok():
    if READY.exists():
        return True
    try:
        import playwright  # noqa: F401
        return True
    except ImportError:
        return False


def logged_in():
    return (HERE / ".browser-profile").exists()


def run_command(opts):
    cmd = [python(), str(SNIPER), "run"]
    days = [d for d in opts.get("days", []) if d in WEEKDAYS]
    if not days:
        raise ValueError("Pick at least one day of the week.")
    cmd += ["--days", ",".join(days)]
    cmd += ["--weeks", str(int(opts.get("weeks") or 4))]
    cmd += ["--interval", str(max(5, float(opts.get("interval") or 30)))]
    if opts.get("time"):
        cmd += ["--time", str(opts["time"])]
    cmd += ["--exclude", str(opts.get("exclude", "")).strip()]
    if opts.get("participant", "").strip():
        cmd += ["--participant", opts["participant"].strip()]
    if opts.get("title", "").strip():
        cmd += ["--title", opts["title"].strip()]
    for flag in ("dry_run", "keep_going", "once", "verbose"):
        if opts.get(flag):
            cmd.append("--" + flag.replace("_", "-"))
    return cmd


# Make a private Python environment in this folder and install Playwright + its browser into it.
SETUP_SCRIPT = f"""
import subprocess, sys, venv
from pathlib import Path
venv_dir, py = Path({str(VENV)!r}), {str(VENV_PY)!r}
print("Creating private Python environment...", flush=True)
venv.create(venv_dir, with_pip=True)
print("Installing Playwright...", flush=True)
subprocess.check_call([py, "-m", "pip", "install", "--upgrade", "playwright"])
print("Downloading the browser (this takes a minute)...", flush=True)
subprocess.check_call([py, "-m", "playwright", "install", "chromium"])
(venv_dir / ".ready").write_text("ok")
print("Setup complete.", flush=True)
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def _json(self, data, code=200):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/":
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith("/status"):
            since = int(self.path.partition("since=")[2] or 0)
            self._json(dict(runner.status(since), deps=deps_ok(), logged_in=logged_in()))
        else:
            self.send_error(404)

    def do_POST(self):
        # Only accept requests from this page (blocks other websites from poking the local server)
        origin = self.headers.get("Origin")
        if origin and origin not in (f"http://localhost:{PORT}", f"http://127.0.0.1:{PORT}"):
            return self.send_error(403)
        length = int(self.headers.get("Content-Length") or 0)
        try:
            opts = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            opts = {}
        try:
            if self.path == "/setup":
                ok = runner.start("setup", [sys.executable, "-c", SETUP_SCRIPT])
            elif self.path == "/login":
                ok = runner.start("login", [python(), str(SNIPER), "login"])
            elif self.path == "/start":
                ok = runner.start("run", run_command(opts))
            elif self.path == "/enter":
                runner.enter()
                ok = True
            elif self.path == "/stop":
                runner.stop()
                ok = True
            else:
                return self.send_error(404)
        except ValueError as e:
            return self._json({"ok": False, "error": str(e)}, 400)
        self._json({"ok": ok, "error": None if ok else "Something is already running. Stop it first."})


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Pickup Sniper</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--ink:#16191d;--muted:#5d6670;--line:#dde1e6;--accent:#0b6e8a;--accent-ink:#fff;--warn:#9a5b00;--ok:#1d7a3a;--log:#0f1419;--log-ink:#d6dde4}
@media (prefers-color-scheme:dark){:root{--bg:#111417;--card:#1a1e23;--ink:#e8ebee;--muted:#9aa4ad;--line:#2c333a;--accent:#3fb3d3;--accent-ink:#06141a;--warn:#e0a24a;--ok:#5cc47e;--log:#0a0d10;--log-ink:#cfd6dc}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
main{max-width:760px;margin:0 auto;padding:24px 16px 48px}
h1{font-size:22px;margin:0 0 4px}
.sub{color:var(--muted);margin:0 0 20px}
section{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin-bottom:14px}
h2{font-size:14px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);margin:0 0 12px}
.row{display:flex;flex-wrap:wrap;gap:12px;align-items:center}
label.field{display:flex;flex-direction:column;gap:4px;font-size:13px;color:var(--muted);flex:1 1 160px}
input[type=text],input[type=number],select{font:inherit;color:var(--ink);background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:7px 9px;width:100%}
.days{display:flex;flex-wrap:wrap;gap:6px}
.days label{border:1px solid var(--line);border-radius:6px;padding:6px 10px;cursor:pointer;user-select:none}
.days input{display:none}
.days label:has(input:checked){background:var(--accent);color:var(--accent-ink);border-color:var(--accent)}
.checks label{display:flex;gap:6px;align-items:center;font-size:14px}
button{font:inherit;border:1px solid var(--line);background:var(--card);color:var(--ink);border-radius:7px;padding:8px 14px;cursor:pointer}
button.primary{background:var(--accent);color:var(--accent-ink);border-color:var(--accent);font-weight:600}
button:disabled{opacity:.45;cursor:default}
.status{display:flex;gap:8px;align-items:center;font-weight:600}
.dot{width:10px;height:10px;border-radius:50%;background:var(--muted)}
.dot.on{background:var(--ok)}
.banner{display:none;border:1px solid var(--warn);color:var(--warn);border-radius:8px;padding:10px 12px;margin-bottom:12px}
.banner.show{display:flex;gap:12px;align-items:center;justify-content:space-between;flex-wrap:wrap}
pre{background:var(--log);color:var(--log-ink);border-radius:8px;padding:12px;height:300px;overflow:auto;font:12.5px/1.45 ui-monospace,Menlo,Consolas,monospace;white-space:pre-wrap;margin:0}
.hint{font-size:13px;color:var(--muted);margin:8px 0 0}
.step{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}
.step + .step{margin-top:10px;padding-top:10px;border-top:1px solid var(--line)}
.done{color:var(--ok);font-weight:600}
</style></head><body><main>
<h1>Pickup Sniper</h1>
<p class="sub">Watches Sharks Ice drop-in hockey and adds open slots to your cart. It never pays. You check out yourself.</p>

<section>
  <h2>First time setup</h2>
  <div class="step"><span>1. Install the browser helper <span id="depsDone" class="done"></span></span>
    <button id="setupBtn">Install</button></div>
  <div class="step"><span>2. Log in to your DaySmart account <span id="loginDone" class="done"></span></span>
    <button id="loginBtn">Open login window</button></div>
</section>

<section>
  <h2>What to look for</h2>
  <div class="days" id="days"></div>
  <div class="row" style="margin-top:12px">
    <label class="field">Start time
      <select id="time"><option value="">Any time</option></select></label>
    <label class="field">Weeks ahead<input id="weeks" type="number" min="1" max="12" value="4"></label>
    <label class="field">Check every (seconds)<input id="interval" type="number" min="5" value="30"></label>
  </div>
  <div class="row" style="margin-top:12px">
    <label class="field">Your name (if the site asks who's playing)<input id="participant" type="text" placeholder="optional"></label>
    <label class="field">Event title<input id="title" type="text" value="OIC - Drop-In Hockey"></label>
  </div>
  <div class="row" style="margin-top:12px">
    <label class="field">Never add slots containing (comma separated)<input id="exclude" type="text" value="Goalie"></label>
  </div>
  <div class="row checks" style="margin-top:12px">
    <label><input type="checkbox" id="dry_run"> Test mode (don't add anything)</label>
    <label><input type="checkbox" id="keep_going"> Keep going after adding slots</label>
    <label><input type="checkbox" id="verbose"> Detailed log</label>
  </div>
</section>

<section>
  <div class="banner" id="banner"><span id="bannerText"></span><button class="primary" id="enterBtn">Done</button></div>
  <div class="row" style="justify-content:space-between">
    <div class="status"><span class="dot" id="dot"></span><span id="statusText">Stopped</span></div>
    <div class="row"><button class="primary" id="startBtn">Start watching</button><button id="stopBtn">Stop</button></div>
  </div>
  <p class="hint">Check out in the browser window this opens, not your usual browser. Your cart lives there.</p>
</section>

<section><h2>Log</h2><pre id="log"></pre></section>
</main>
<script>
const $ = id => document.getElementById(id);
const DAYS = [["mon","Mon"],["tue","Tue"],["wed","Wed"],["thu","Thu"],["fri","Fri"],["sat","Sat"],["sun","Sun"]];
$("days").innerHTML = DAYS.map(([v,l]) => `<label><input type="checkbox" value="${v}">${l}</label>`).join("");
for (let h = 5; h <= 22; h++) for (const m of [0, 15, 30, 45]) {
  const label = `${h % 12 || 12}:${String(m).padStart(2,"0")} ${h < 12 ? "AM" : "PM"}`;
  $("time").insertAdjacentHTML("beforeend", `<option value="${label.replace(" ","").toLowerCase()}">${label}</option>`);
}
const FIELDS = ["exclude", "time","weeks","interval","participant","title","dry_run","keep_going","verbose"];
function read() {
  const o = {days: [...document.querySelectorAll("#days input:checked")].map(i => i.value)};
  for (const f of FIELDS) { const el = $(f); o[f] = el.type === "checkbox" ? el.checked : el.value; }
  return o;
}
function load() {
  let s = null; try { s = JSON.parse(localStorage.getItem("sniper") || "null"); } catch (e) {}
  s = s || {days: ["wed","fri"], time: "6:00am"};
  document.querySelectorAll("#days input").forEach(i => i.checked = (s.days || []).includes(i.value));
  for (const f of FIELDS) if (f in s) { const el = $(f); if (el.type === "checkbox") el.checked = s[f]; else el.value = s[f]; }
}
function save() { try { localStorage.setItem("sniper", JSON.stringify(read())); } catch (e) {} }
load();
document.querySelectorAll("input,select").forEach(el => el.addEventListener("change", save));

async function post(path, body) {
  const r = await fetch(path, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body || {})});
  const d = await r.json().catch(() => ({}));
  if (d.error) alert(d.error);
  poll();
}
$("setupBtn").onclick = () => post("/setup");
$("loginBtn").onclick = () => post("/login");
$("startBtn").onclick = () => post("/start", read());
$("stopBtn").onclick = () => post("/stop");
$("enterBtn").onclick = () => post("/enter");

let since = 0;
async function poll() {
  let s; try { s = await (await fetch("/status?since=" + since)).json(); } catch (e) { $("statusText").textContent = "Control panel closed"; return; }
  if (s.log) { const pre = $("log"); const atEnd = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 20;
    pre.textContent += s.log; if (atEnd) pre.scrollTop = pre.scrollHeight; }
  since = s.size;
  const names = {setup: "Installing…", login: "Login window open", run: "Watching for slots"};
  $("dot").classList.toggle("on", s.running);
  $("statusText").textContent = s.running ? names[s.mode] : "Stopped";
  ["setupBtn","loginBtn","startBtn"].forEach(b => $(b).disabled = s.running);
  $("stopBtn").disabled = !s.running || s.mode !== "run";
  $("depsDone").textContent = s.deps ? "✓ done" : "";
  $("loginDone").textContent = s.logged_in ? "✓ done" : "";
  $("banner").classList.toggle("show", s.waiting);
  $("bannerText").textContent = s.mode === "login"
    ? "Log in using the browser window that opened, then click Done."
    : "Slots are in your cart! Finish checkout in the browser window, then click Done to close it.";
  if (s.waiting && !document.hasFocus()) document.title = "🏒 Check out now! – Pickup Sniper"; else document.title = "Pickup Sniper";
}
setInterval(poll, 1000); poll();
</script></body></html>
"""


def main():
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://localhost:{PORT}"
    print(f"Pickup Sniper is running at {url}  (close this window to quit)")
    threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        runner.stop()


if __name__ == "__main__":
    main()
