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
    """Runs one child process at a time (setup, or the browser session) and collects its output.

    The session is one long-lived browser: login, confirming it, and watching all happen in it,
    driven by JSON commands on its stdin. It reports its state with "@@NAME value" lines."""

    def __init__(self):
        self.lock = threading.Lock()
        self.proc = None
        self.mode = None
        self.log = []
        self.state = {}

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self, mode, cmd):
        with self.lock:
            if self.running():
                return False
            self.mode = mode
            self.state = {"browser": False, "login": None, "watching": False, "checkout": False}
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
        for raw in iter(proc.stdout.readline, b""):
            line = raw.decode("utf-8", "replace").replace("\a", "")
            with self.lock:
                if line.startswith("@@"):
                    name, _, value = line[2:].strip().partition(" ")
                    if name == "BROWSER":
                        self.state["browser"] = value == "open"
                    elif name == "LOGIN":
                        self.state["login"] = value
                    elif name == "WATCHING":
                        self.state["watching"] = value == "on"
                    elif name == "CHECKOUT":
                        self.state["checkout"] = True
                    continue
                self.log.append(line)
                del self.log[:-2000]
        proc.wait()
        with self.lock:
            self.state = {"browser": False, "login": None, "watching": False, "checkout": False}
            self.log.append(f"[finished: {self.mode}]\n")

    def send(self, msg):
        with self.lock:
            if not self.running() or self.mode != "session":
                return False
            try:
                self.proc.stdin.write((json.dumps(msg) + "\n").encode())
                self.proc.stdin.flush()
            except OSError:
                return False
            if msg["cmd"] == "check":
                self.state["login"] = "checking"
            elif msg["cmd"] == "start":
                self.state["watching"] = True
                self.state["checkout"] = False
            return True

    def session_state(self):
        with self.lock:
            return dict(self.state) if self.running() and self.mode == "session" else {}

    def shutdown(self):
        if self.running():
            if self.mode == "session":
                self.send({"cmd": "quit"})
                try:
                    self.proc.wait(timeout=5)
                    return
                except subprocess.TimeoutExpired:
                    pass
            self.proc.terminate()

    def status(self, since):
        with self.lock:
            text = "".join(self.log)
            return {
                "running": self.running(),
                "mode": self.mode if self.running() else None,
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



def run_args(opts):
    """Panel settings -> `sniper.py run` options."""
    cmd = []
    days = [d for d in opts.get("days", []) if d in WEEKDAYS]
    if not days:
        raise ValueError("Pick at least one day of the week.")
    cmd += ["--days", ",".join(days)]
    cmd += ["--weeks", str(int(opts.get("weeks") or 4))]
    cmd += ["--interval", str(max(5, float(opts.get("interval") or 30)))]
    if opts.get("time"):
        cmd += ["--time", str(opts["time"])]
    cmd += ["--exclude", str(opts.get("exclude", "")).strip()]
    keywords = str(opts.get("keywords", "")).strip()
    if not any(c.isalnum() for c in keywords):
        raise ValueError("Enter at least one keyword, like Drop-In Hockey.")
    cmd += ["--keywords", keywords]
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
            self._json(dict(runner.status(since), deps=deps_ok(), session=runner.session_state()))
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
            elif self.path == "/open":
                ok = runner.start("session", [python(), str(SNIPER), "session"])
                if not ok:
                    raise ValueError("The browser is already open (or setup is still running).")
            elif self.path == "/check":
                ok = runner.send({"cmd": "check"})
                if not ok:
                    raise ValueError("Open the browser first.")
            elif self.path == "/start":
                st = runner.session_state()
                if not st.get("browser"):
                    raise ValueError("Open the browser and log in first.")
                if st.get("login") not in ("in", "unknown"):
                    raise ValueError("Log in to DaySmart in the browser, then click \"I'm logged in\" first.")
                ok = runner.send({"cmd": "start", "argv": run_args(opts)})
            elif self.path == "/stop":
                ok = runner.send({"cmd": "stop"})
            elif self.path == "/close":
                ok = runner.send({"cmd": "quit"})
            else:
                return self.send_error(404)
        except ValueError as e:
            return self._json({"ok": False, "error": str(e)}, 400)
        self._json({"ok": ok, "error": None if ok else "Something is already running."})


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
.bad{color:var(--warn);font-weight:600}
.unsure{color:var(--muted);font-weight:600}
</style></head><body><main>
<h1>Pickup Sniper</h1>
<p class="sub">Watches Sharks Ice drop-in hockey and adds open slots to your cart. It never pays. You check out yourself.</p>

<section>
  <h2>1. First time setup</h2>
  <div class="step"><span>Install the browser helper <span id="depsDone" class="done"></span></span>
    <button id="setupBtn">Install</button></div>
</section>

<section>
  <h2>2. Log in</h2>
  <div class="step"><span>Open the browser and log in to DaySmart there <span id="browserState" class="done"></span></span>
    <span class="row"><button id="openBtn" class="primary">Open browser</button><button id="closeBtn">Close browser</button></span></div>
  <div class="step"><span>Then come back here and confirm <span id="loginState"></span></span>
    <button id="checkBtn">I'm logged in</button></div>
</section>

<section>
  <h2>3. What to look for</h2>
  <div class="days" id="days"></div>
  <div class="row" style="margin-top:12px">
    <label class="field">Start time
      <select id="time"><option value="">Any time</option></select></label>
    <label class="field">Weeks ahead<input id="weeks" type="number" min="1" max="12" value="4"></label>
    <label class="field">Check every (seconds)<input id="interval" type="number" min="5" value="30"></label>
  </div>
  <div class="row" style="margin-top:12px">
    <label class="field">Slot must contain (comma separated)<input id="keywords" type="text" value="Drop-In Hockey"></label>
    <label class="field">Never add slots containing (comma separated)<input id="exclude" type="text" value="Goalie"></label>
  </div>
  <div class="row checks" style="margin-top:12px">
    <label><input type="checkbox" id="dry_run"> Test mode (don't add anything)</label>
    <label><input type="checkbox" id="keep_going"> Keep going after adding slots</label>
    <label><input type="checkbox" id="verbose"> Detailed log</label>
  </div>
</section>

<section>
  <h2>4. Watch</h2>
  <div class="banner" id="banner"><span>Slots are in your cart! Finish checkout in the browser window. Your cart lives there, not in your usual browser.</span></div>
  <div class="row" style="justify-content:space-between">
    <div class="status"><span class="dot" id="dot"></span><span id="statusText">Stopped</span></div>
    <div class="row"><button class="primary" id="startBtn">Start watching</button><button id="stopBtn">Stop</button></div>
  </div>
  <p class="hint" id="startHint"></p>
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
const FIELDS = ["exclude", "time","weeks","interval","keywords","dry_run","keep_going","verbose"];
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
$("openBtn").onclick = () => post("/open");
$("closeBtn").onclick = () => post("/close");
$("checkBtn").onclick = () => post("/check");
$("startBtn").onclick = () => post("/start", read());
$("stopBtn").onclick = () => post("/stop");

let since = 0;
async function poll() {
  let s; try { s = await (await fetch("/status?since=" + since)).json(); } catch (e) { $("statusText").textContent = "Control panel closed"; return; }
  if (s.log) { const pre = $("log"); const atEnd = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 20;
    pre.textContent += s.log; if (atEnd) pre.scrollTop = pre.scrollHeight; }
  since = s.size;
  const S = s.session || {}, setup = s.running && s.mode === "setup";
  const loggedIn = S.login === "in" || S.login === "unknown";
  $("dot").classList.toggle("on", !!S.watching || setup);
  $("statusText").textContent = setup ? "Installing…" : S.watching ? "Watching for slots" : "Not watching";
  $("depsDone").textContent = s.deps ? "✓ done" : "";
  $("setupBtn").disabled = s.running;
  $("openBtn").disabled = s.running;
  $("closeBtn").disabled = !S.browser || S.watching;
  $("browserState").textContent = S.browser ? "✓ browser open" : "";
  $("checkBtn").disabled = !S.browser || S.watching || S.login === "checking";
  const [cls, txt] = !S.browser ? ["unsure", ""]
    : S.login === "checking" ? ["unsure", "checking…"]
    : S.login === "in" ? ["done", "✓ logged in"]
    : S.login === "out" ? ["bad", "✗ not logged in yet: log in, then click again"]
    : S.login === "unknown" ? ["unsure", "? couldn't confirm, but you can start if you are logged in"]
    : ["unsure", ""];
  $("loginState").className = cls; $("loginState").textContent = txt;
  $("startBtn").disabled = !S.browser || !loggedIn || S.watching;
  $("stopBtn").disabled = !S.watching;
  $("startHint").textContent = !S.browser ? "Open the browser and log in first (step 2)."
    : !loggedIn ? "Click \"I'm logged in\" in step 2 first." : "";
  $("banner").classList.toggle("show", !!S.checkout && !S.watching);
  document.title = S.checkout && !S.watching && !document.hasFocus() ? "🏒 Check out now! – Pickup Sniper" : "Pickup Sniper";
}
setInterval(poll, 1000); poll();
</script></body></html>
"""


def self_update():
    """If this folder is a git checkout, pull the latest version, and restart if anything changed."""
    if not (HERE / ".git").exists():
        return

    def git(*args):
        return subprocess.run(["git", *args], cwd=HERE, capture_output=True, text=True, timeout=60)

    try:
        before = git("rev-parse", "HEAD").stdout.strip()
        pull = git("pull", "--ff-only", "--quiet")
        after = git("rev-parse", "HEAD").stdout.strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"Couldn't check for updates ({e}); using the current version.")
        return
    if pull.returncode != 0:
        print("Couldn't check for updates; using the current version.\n" + pull.stderr.strip())
    elif before != after:
        print("Updated to the latest version. Restarting...")
        os.execv(sys.executable, [sys.executable, *sys.argv])
    else:
        print("Already up to date.")


def main():
    self_update()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://localhost:{PORT}"
    print(f"Pickup Sniper is running at {url}  (close this window to quit)")
    threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        runner.shutdown()


if __name__ == "__main__":
    main()
