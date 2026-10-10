#!/usr/bin/env python3
"""Poll DaySmart (Sharks Ice) event registration and add matching pickup slots to the cart.

Usage:
  python sniper.py login                      # one time: log in by hand, session is saved
  python sniper.py run                        # upcoming Wednesdays and Fridays, next 4 weeks
  python sniper.py run --time 6am             # only slots that start at 6:00 AM

The script only ADDS TO CART. It never checks out or pays.
"""
import argparse
import datetime as dt
import json
import queue
import random
import re
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urljoin

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

BASE_URL = "https://apps.daysmartrecreation.com/dash/x/sharks/event-registration"
CART_URL = "https://apps.daysmartrecreation.com/dash/x/sharks/cart"
QUERY = "facility_ids=3&sport_ids=32&sport_ids=20&sport_ids=44"
DEFAULT_KEYWORDS = "Drop-In Hockey"

HERE = Path(__file__).resolve().parent
PROFILE_DIR = HERE / ".browser-profile"
OLD_STATE_FILE = HERE / "secured.json"  # no longer used; removed on startup
SHOTS_DIR = HERE / "shots"
DEBUG_DIR = HERE / "debug"

# Button text that means "I can book this"
BOOK_RE = re.compile(r"^\s*(register|add to cart|book|sign up|select|reserve)\b", re.I)
# Text on a card that means it's not bookable right now
UNAVAILABLE_RE = re.compile(r"sold out|\bfull\b|wait ?list|not (yet )?available|registration (opens|closed)", re.I)
# Buttons that move a follow-up dialog forward
CONFIRM_RE = re.compile(r"^\s*(add to cart|add|continue|next|confirm|register|save)\b", re.I)
SUCCESS_RE = re.compile(r"added to (your )?cart|item added|added to your order", re.I)


def log(msg):
    print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", flush=True)


WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def daterange(start, end, weekdays):
    d = start
    while d <= end:
        if d.weekday() in weekdays:
            yield d
        d += dt.timedelta(days=1)


def parse_weekdays(text):
    out = set()
    for part in text.lower().split(","):
        part = part.strip()[:3]
        if part not in WEEKDAYS:
            sys.exit(f"--days: don't know {part!r}; use e.g. wed,fri")
        out.add(WEEKDAYS.index(part))
    return out


def shot(page, name):
    SHOTS_DIR.mkdir(exist_ok=True)
    path = SHOTS_DIR / f"{dt.datetime.now():%Y%m%d-%H%M%S}-{name}.png"
    try:
        page.screenshot(path=str(path), full_page=True)
    except Exception:
        pass
    return path


def dump(name, html):
    DEBUG_DIR.mkdir(exist_ok=True)
    path = DEBUG_DIR / f"{dt.datetime.now():%Y%m%d-%H%M%S}-{name}.html"
    path.write_text(html)
    return path


def open_context(p, headless):
    ctx = p.chromium.launch_persistent_context(
        str(PROFILE_DIR), headless=headless, viewport={"width": 1280, "height": 1000}
    )
    restore_auth(ctx)
    return ctx


TIME_RE = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?(?:m\.?)?(?![a-z])", re.I)


def parse_time(text):
    m = TIME_RE.fullmatch(text.strip())
    if not m:
        raise argparse.ArgumentTypeError(f"can't read time {text!r}; use e.g. 6am or 6:00am")
    return to_time(m)


def to_time(m):
    hour = int(m.group(1)) % 12 + (12 if m.group(3).lower() == "p" else 0)
    return dt.time(hour, int(m.group(2) or 0))


def start_time(card_text):
    """The first time printed on the card, i.e. when the slot starts."""
    m = TIME_RE.search(card_text)
    return to_time(m) if m else None


def normalize(text):
    """Lowercase letters and digits only, so 'Drop-In', 'drop in' and 'DropIn' all compare equal."""
    return re.sub(r"[^a-z0-9]", "", text.lower())


def parse_keywords(text):
    words = [w.strip() for w in text.split(",") if normalize(w)]
    if not words:
        raise argparse.ArgumentTypeError("give at least one keyword")
    return words


def has_all(text, keywords):
    flat = normalize(text)
    return all(normalize(k) in flat for k in keywords)


AUTH_FILE = HERE / ".auth.json"  # saved cookies, including the ones a browser normally drops on close
LOGGED_IN_RE = re.compile(r"\b(log ?out|sign ?out|log ?off|my account|my profile|logged in as)\b", re.I)
LOGGED_OUT_RE = re.compile(r"\b(log ?in|sign ?in)\b", re.I)


def page_labels(page):
    """Text (or aria-label) of every visible link and button on the page."""
    return page.evaluate("""() => [...document.querySelectorAll('a, button, [role=button]')]
        .filter(e => e.offsetParent !== null || e.getClientRects().length)
        .map(e => (e.innerText || e.getAttribute('aria-label') || e.title || '').replace(/\\s+/g, ' ').trim())
        .filter(t => t)""")


def login_state(page, settle=0.0, report=False):
    """'in', 'out' or 'unknown'.

    Logged out = the page offers a Log In / Sign In link. Logged in = it offers Log Out / My Account,
    or (once the page has had `settle` seconds to finish drawing) there's simply no Log In link."""
    deadline = time.time() + settle
    while True:
        try:
            labels = page_labels(page)
        except Exception:
            labels = []  # mid-navigation
        short = [t for t in labels if len(t) <= 40]
        if re.search(r"\b(log ?in|sign ?in)\b", page.url, re.I) or any(LOGGED_OUT_RE.search(t) for t in short):
            state = "out"
            break
        if any(LOGGED_IN_RE.search(t) for t in short):
            state = "in"
            break
        if time.time() >= deadline:
            state = "in" if labels else "unknown"  # page has drawn its links and none of them is Log In
            break
        page.wait_for_timeout(500)
    if report:
        log(f"  Login check on {page.url}")
        log(f"  Links/buttons seen: {', '.join(repr(t) for t in short[:30]) or '(none)'}")
    return state


def save_auth(ctx):
    try:
        AUTH_FILE.write_text(json.dumps(ctx.storage_state()["cookies"]))
        AUTH_FILE.chmod(0o600)
    except Exception:
        pass


def restore_auth(ctx):
    try:
        ctx.add_cookies(json.loads(AUTH_FILE.read_text()))
    except Exception:
        pass


_stdin_lines = queue.Queue()


def wait_for_enter(timeout=None):
    """True if Enter was pressed (or the control panel's button clicked) within timeout seconds."""
    if not getattr(wait_for_enter, "started", False):
        def reader():
            for line in sys.stdin:
                _stdin_lines.put(line)
            _stdin_lines.put(None)
        threading.Thread(target=reader, daemon=True).start()
        wait_for_enter.started = True
    try:
        return _stdin_lines.get(timeout=timeout) is not None
    except queue.Empty:
        return False


def ensure_logged_in(page, url):
    """Make sure we're logged in, in THIS browser. If not, wait here until the user logs in."""
    if login_state(page) == "in":
        save_auth(page.context)
        return
    log("LOGIN NEEDED: log in to DaySmart in the browser window that just opened. "
        "Watching starts by itself once you're logged in (or press Enter here if it doesn't notice).")
    print("\a", end="", flush=True)
    while True:
        if wait_for_enter(timeout=3):
            log("Continuing.")
            break
        try:
            if login_state(page) == "in":
                log("Logged in to DaySmart.")
                break
        except Exception:
            pass  # page is mid-navigation while they log in
    save_auth(page.context)
    load_page(page, url)


def load_page(page, url):
    page.goto(url, wait_until="domcontentloaded")
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except PWTimeout:
        pass
    page.wait_for_timeout(1000)


def cmd_login(_args):
    with sync_playwright() as p:
        ctx = open_context(p, headless=False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        load_page(page, f"{BASE_URL}?{QUERY}")
        ensure_logged_in(page, f"{BASE_URL}?{QUERY}")
        ctx.close()


# Marks the smallest elements whose text contains every keyword (normally the event title line).
MARK_ANCHORS_JS = """
(keywords) => {
  const norm = t => (t || "").toLowerCase().replace(/[^a-z0-9]/g, "");
  const keys = keywords.map(norm);
  const hit = el => { const t = norm(el.innerText); return keys.every(k => t.includes(k)); };
  document.querySelectorAll("[data-sniper-anchor]").forEach(e => e.removeAttribute("data-sniper-anchor"));
  let n = 0;
  for (const el of document.body.querySelectorAll("*")) {
    if (["SCRIPT", "STYLE", "NOSCRIPT"].includes(el.tagName) || !hit(el)) continue;
    if ([...el.children].some(hit)) continue;  // a child matches too, so this isn't the smallest
    el.setAttribute("data-sniper-anchor", String(n++));
  }
  return n;
}
"""


def event_cards(page, keywords):
    """Return ([(card_locator, card_text)], n_without_button) for event cards containing all the keywords."""
    count = page.evaluate(MARK_ANCHORS_JS, keywords)
    no_button = 0
    cards = []
    seen = set()
    for i in range(count):
        el = page.locator(f"[data-sniper-anchor='{i}']")
        # Climb to the nearest ancestor that also contains a button/link: that's the event card.
        card = el.locator("xpath=ancestor-or-self::*[.//button or .//a[@role='button' or contains(@class,'btn')]][1]")
        if card.count() == 0:
            no_button += 1
            continue
        card = card.first
        text = " ".join(card.inner_text().split())
        if text in seen:
            continue
        seen.add(text)
        # If the climb swallowed several events (more times than one start/end per title), it's not one card.
        anchors = card.locator("[data-sniper-anchor]").count() or 1
        if len(TIME_RE.findall(text)) > 2 * anchors:
            no_button += 1
            continue
        cards.append((card, text))
    return cards, no_button


def book_button(card):
    for btn in card.locator("button, a[role='button'], a.btn, [class*='btn']").all():
        try:
            if btn.is_visible() and btn.is_enabled() and BOOK_RE.search(btn.inner_text()):
                return btn
        except Exception:
            continue
    return None


def finish_dialog(page, participant, exclude, before_text, before_url):
    """Click through whatever comes after the first Register click until the item is in the cart.

    Success means NEW "added to cart" text appeared, or we were taken to the cart page;
    text that was already on the page before clicking doesn't count."""
    baseline = len(SUCCESS_RE.findall(before_text))
    was_cart = "cart" in before_url.lower()

    def succeeded():
        return len(SUCCESS_RE.findall(page.inner_text("body"))) > baseline or (
            not was_cart and "cart" in page.url.lower())

    for _ in range(5):
        page.wait_for_timeout(1200)
        if succeeded():
            return True
        if participant:
            who = page.get_by_text(participant, exact=False)
            for el in who.all():
                try:
                    if el.is_visible():
                        el.click()
                        break
                except Exception:
                    continue
        clicked = False
        dialog = page.locator("[role='dialog'], .modal.show, .modal-dialog, .v-dialog--active")
        scope = dialog.last if dialog.count() else page
        for btn in scope.get_by_role("button").all():
            try:
                label = btn.inner_text()
                if any(w.lower() in label.lower() for w in exclude):
                    continue
                if btn.is_visible() and btn.is_enabled() and CONFIRM_RE.search(label):
                    btn.click()
                    clicked = True
                    break
            except Exception:
                continue
        if not clicked:
            break
    page.wait_for_timeout(1500)
    return succeeded()


LAST_SUMMARY = {}


def fmt_time(t):
    return f"{t.hour % 12 or 12}:{t.minute:02d} {'AM' if t.hour < 12 else 'PM'}"


class LoggedOut(Exception):
    pass


def scan_day(page, day, args, secured):
    url = f"{BASE_URL}?date={day.isoformat()}&{QUERY}"
    load_page(page, url)
    if login_state(page) == "out":
        raise LoggedOut()

    cards, no_button = event_cards(page, args.keywords)
    added = 0
    reasons = {"no Register button yet": no_button} if no_button else {}

    def skip(reason, text):
        reasons[reason] = reasons.get(reason, 0) + 1
        if args.verbose:
            log(f"  skipping ({reason}): {text[:100]}")

    for card, text in cards:
        key = f"{day}|{text[:120]}"
        if args.verbose:
            buttons = [" ".join(b.inner_text().split()) for b in card.locator("button, a").all()]
            log(f"  card: {text[:100]}")
            log(f"    buttons/links: {buttons}")
            dump(f"{day}-card", card.evaluate("e => e.outerHTML"))
        if key in secured:
            skip("already added this session", text)
            continue
        blocked = [w for w in args.exclude if normalize(w) and normalize(w) in normalize(text)]
        if blocked:
            skip(f"contains {blocked[0]!r}", text)
            continue
        if args.time:
            begins = start_time(text)
            if begins != args.time:
                skip(f"starts at {fmt_time(begins) if begins else 'unknown time'}", text)
                continue
        if UNAVAILABLE_RE.search(text):
            skip("sold out / not open", text)
            continue
        btn = book_button(card)
        if not btn:
            skip("no Register button yet", text)
            continue

        log(f"AVAILABLE on {day}: {text[:120]}")
        if args.dry_run:
            log("  dry run, not clicking")
            continue
        before_text, before_url = page.inner_text("body"), page.url
        btn.click()
        ok = finish_dialog(page, args.participant, args.exclude, before_text, before_url)
        path = shot(page, f"{day}-{'added' if ok else 'check'}")
        if not ok:
            dump(f"{day}-after-click", page.content())
        if ok:
            log(f"  ADDED TO CART. Go check out! (screenshot: {path})")
            print("\a", end="", flush=True)
            secured.add(key)
            added += 1
        else:
            log(f"  Clicked Register but couldn't confirm it's in the cart. Look at {path}")
        # Reload the day so the remaining cards are fresh
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_timeout(1500)
        page.evaluate(MARK_ANCHORS_JS, args.keywords)

    summary = f"{day:%a %b %d}: {len(cards) + no_button} matching slot(s)"
    if reasons:
        summary += ", skipped " + ", ".join(f"{n} {r}" for r, n in reasons.items())
    if args.verbose or summary != LAST_SUMMARY.get(day):
        log(summary)
    LAST_SUMMARY[day] = summary
    return added, len(cards) + no_button


SELECT_REGISTRANTS_RE = re.compile(r"select\s+registrants?", re.I)
# Each pending registration page has one of these; "Confirm Registration" shows up e.g. when the
# person is already registered for that event.
NEXT_REGISTRANT_RE = re.compile(r"next\s+registrant|confirm\s+registration", re.I)


# Sites often grey a button out with aria-disabled or a "disabled" class instead of real disabling.
NOT_GREYED_OUT_JS = """e => !e.closest('[disabled], [aria-disabled="true"], .disabled, .is-disabled')"""


def find_button(tab, name_re, timeout=0):
    """A visible, clickable element labelled name_re (button, link, or anything else), waiting up to timeout s."""
    deadline = time.time() + timeout
    while True:
        candidates = (tab.get_by_role("button", name=name_re).all() + tab.get_by_role("link", name=name_re).all()
                      + tab.get_by_text(name_re).all())
        for el in candidates:
            try:
                if el.is_visible() and el.is_enabled() and el.evaluate(NOT_GREYED_OUT_JS):
                    return el
            except Exception:
                continue
        if time.time() >= deadline:
            return None
        tab.wait_for_timeout(500)


def wait_for_change(tab, before, timeout=15):
    """Wait until the page text differs from `before` (the click took us to the next page)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        tab.wait_for_timeout(500)
        try:
            if tab.inner_text("body") != before:
                tab.wait_for_timeout(1000)  # let the rest of the new page render
                return True
        except Exception:
            pass  # mid-navigation
    return False


def newest_tab(ctx, tab, pages_before):
    """If the click opened a new tab, carry on in that one."""
    new = [p for p in ctx.pages if p not in pages_before and not p.is_closed()]
    if new:
        new[-1].wait_for_load_state("domcontentloaded")
        return new[-1]
    return tab


def click_next_registrants(ctx, tab):
    """DaySmart shows each pending registration on its own page; click "Next Registrant" until it stops asking."""
    clicks = 0
    for _ in range(40):
        btn = find_button(tab, NEXT_REGISTRANT_RE, timeout=15 if clicks == 0 else 8)
        if btn is None:
            break
        label = " ".join(btn.inner_text().split())
        before, pages_before = tab.inner_text("body"), list(ctx.pages)
        moved = False
        for attempt in range(4):  # if a click doesn't take, wait and try again
            btn.click()
            tab = newest_tab(ctx, tab, pages_before)
            if wait_for_change(tab, before, timeout=6):
                moved = True
                break
            btn = find_button(tab, NEXT_REGISTRANT_RE, timeout=5)
            if btn is None:
                break
        if not moved:
            log(f"  Clicked {label!r} but the page didn't move on; stopping here.")
            break
        log(f"  Clicked {label!r}.")
        clicks += 1
    return clicks, tab


def open_checkout(ctx, page, cart_tab, keywords):
    """After a round of adding, open the cart in its own tab and click through the registrant pages."""
    href = None
    for a in page.locator("a[href*='cart' i], a[href*='checkout' i]").all():
        try:
            href = a.get_attribute("href")
            if href:
                break
        except Exception:
            continue
    url = urljoin(page.url, href) if href else CART_URL
    if cart_tab is None or cart_tab.is_closed():
        cart_tab = ctx.new_page()
    load_page(cart_tab, url)
    cart_tab.bring_to_front()
    select = find_button(cart_tab, SELECT_REGISTRANTS_RE, timeout=15)
    if select is not None:
        before, pages_before = cart_tab.inner_text("body"), list(ctx.pages)
        select.click()
        log("  Clicked Select Registrants.")
        cart_tab = newest_tab(ctx, cart_tab, pages_before)
        wait_for_change(cart_tab, before)
    else:
        log("  Didn't find a Select Registrants button on the cart page.")
    clicks, cart_tab = click_next_registrants(ctx, cart_tab)
    if not clicks:
        log("  No Next Registrant / Confirm Registration button showed up.")
        dump("registrants", cart_tab.content())
    cart_tab.bring_to_front()
    shot(cart_tab, "checkout")
    if has_all(cart_tab.inner_text("body"), keywords):
        log("  Confirmed: the slot shows up in your cart.")
    else:
        log("  WARNING: the cart page doesn't seem to list the slot. It may not have been added; check the browser.")
    log(f"  Ready for you to check out in the browser: {cart_tab.url}")
    return cart_tab


def upcoming_days(args, weekdays):
    """Dates to check right now. Recomputed every round so the window rolls forward on its own."""
    today = dt.date.today()
    start = max(today, dt.date.fromisoformat(args.start)) if args.start else today
    end = dt.date.fromisoformat(args.end) if args.end else today + dt.timedelta(weeks=args.weeks)
    return list(daterange(start, end, weekdays))


def watch(ctx, page, args, stop, on_logged_out):
    """Work through the dates IN ORDER. Sit on the current date, reloading just that page, until its
    slots are published; add what's open; move straight on to the next date. As soon as we reach a
    date that isn't published yet after adding something, go to checkout and stop.

    Returns the cart tab if anything was added, else None. on_logged_out(page) is called if DaySmart
    logs us out; return True to carry on, False to stop watching."""
    weekdays = parse_weekdays(args.days)
    secured = set()  # slots added during this run, so we don't add the same one twice
    OLD_STATE_FILE.unlink(missing_ok=True)
    days = upcoming_days(args, weekdays)
    if not days:
        log(f"No upcoming {args.days} dates to check.")
        return None
    log(f"Looking for slots containing {' + '.join(args.keywords)}"
        + (f" starting at {fmt_time(args.time)}" if args.time else "")
        + (" (DRY RUN)" if args.dry_run else ""))
    log(f"Dates, in order: {', '.join(f'{d:%a %b %d}' for d in days)}")
    cart_tab = None
    total_added = 0
    i = 0
    waiting_on = None
    checks = 0
    while not stop.is_set() and i < len(days):
        day = days[i]
        try:
            added, published = scan_day(page, day, args, secured)
        except LoggedOut:
            if not on_logged_out(page):
                return cart_tab
            continue
        except PWTimeout:
            log(f"{day}: page timed out, retrying")
            continue
        except Exception as e:
            log(f"{day}: error {e!r}")
            stop.wait(2)
            continue
        total_added += added
        if published or args.once:
            i += 1  # this date is out (whether or not anything could be added): next one, right away
            waiting_on = None
            continue
        if total_added:
            break  # got something, and the next date isn't out yet: don't hold the cart, go check out
        if waiting_on != day:
            waiting_on, checks = day, 0
            log(f"Waiting for {day:%a %b %d} to be published; reloading it every ~{args.interval:g}s.")
        checks += 1
        if checks % 60 == 0:
            log(f"Still waiting for {day:%a %b %d} ({checks} checks so far).")
        stop.wait(args.interval + random.uniform(0, args.interval * 0.2))
    if i >= len(days) and not total_added:
        log("Went through every date; nothing to add.")
    if total_added and not stop.is_set():
        try:
            cart_tab = open_checkout(ctx, page, cart_tab, args.keywords)
        except Exception as e:
            log(f"Couldn't open the cart ({e!r}). Open it yourself in the browser.")
            cart_tab = page
        log("Reached checkout. Stopped watching so you can pay.")
    return cart_tab


def cmd_run(args):
    """Command-line use: one browser, asks you to log in if needed, then watches."""
    with sync_playwright() as p:
        ctx = open_context(p, headless=args.headless)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        cart_tab = None
        try:
            load_page(page, f"{BASE_URL}?{QUERY}")
            if login_state(page) == "out":
                if args.headless:
                    sys.exit("You're logged out of DaySmart. Run without --headless so you can log in.")
                ensure_logged_in(page, f"{BASE_URL}?{QUERY}")

            def relogin(pg):
                if args.headless:
                    log("You're logged out of DaySmart. Run without --headless so you can log in.")
                    return False
                ensure_logged_in(pg, pg.url)
                return True

            cart_tab = watch(ctx, page, args, threading.Event(), relogin)
        except KeyboardInterrupt:
            log("stopped")
        finally:
            if cart_tab is not None and not args.headless:
                print("\a", end="", flush=True)
                print("Slots are in your cart. Finish checkout in the browser, then press Enter here to close it... ",
                      flush=True)
                try:
                    wait_for_enter()
                except KeyboardInterrupt:
                    pass
            ctx.close()


def marker(name, value=""):
    """Status lines for the control panel (it hides them from the log)."""
    print(f"@@{name} {value}".rstrip(), flush=True)


def cmd_session(_args):
    """For the control panel: keep ONE browser open and take commands (JSON lines) on stdin.

    {"cmd": "check"}              check whether we're logged in to DaySmart
    {"cmd": "start", "argv": []}  start watching with these `run` options
    {"cmd": "stop"}               stop watching (browser stays open)
    {"cmd": "quit"}               close the browser and exit
    """
    commands = queue.Queue()
    stop = threading.Event()

    def reader():
        for line in sys.stdin:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("cmd") in ("stop", "quit"):
                stop.set()
            commands.put(msg)
        stop.set()
        commands.put({"cmd": "quit"})
    threading.Thread(target=reader, daemon=True).start()

    with sync_playwright() as p:
        ctx = open_context(p, headless=False)
        closed = threading.Event()
        ctx.on("close", lambda _: closed.set())
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        load_page(page, f"{BASE_URL}?{QUERY}")
        log("Browser open. Log in to DaySmart there, then click \"I'm logged in\" in the control panel.")
        marker("BROWSER", "open")

        def live_page():
            nonlocal page
            if page.is_closed():
                page = ctx.new_page()
                load_page(page, f"{BASE_URL}?{QUERY}")
            return page

        def on_logged_out(_pg):
            log("DaySmart logged you out. Log in again in the browser, confirm in the control panel, then start again.")
            marker("LOGIN", "out")
            return False

        while not closed.is_set():
            try:
                msg = commands.get(timeout=1)
            except queue.Empty:
                continue
            cmd = msg.get("cmd")
            try:
                if cmd == "quit":
                    break
                elif cmd == "check":
                    pg = live_page()
                    state = login_state(pg, settle=3, report=True)
                    if state != "in":  # they may be on some other page; look at the event page itself
                        load_page(pg, f"{BASE_URL}?{QUERY}")
                        state = login_state(pg, settle=5, report=True)
                    if state == "in":
                        save_auth(ctx)
                    log({"in": "Logged in to DaySmart.",
                         "out": "Not logged in yet: DaySmart still shows a Log In link.",
                         "unknown": "Couldn't tell for sure whether you're logged in."}[state])
                    marker("LOGIN", state)
                elif cmd == "start":
                    stop.clear()
                    try:
                        args = make_parser().parse_args(["run", *msg.get("argv", [])])
                    except SystemExit:
                        log("Bad settings; not starting.")
                        continue
                    marker("WATCHING", "on")
                    cart_tab = watch(ctx, live_page(), args, stop, on_logged_out)
                    if stop.is_set():
                        log("Stopped watching. The browser stays open.")
                    if cart_tab is not None:
                        cart_tab.bring_to_front()
                        print("\a", end="", flush=True)
                        marker("CHECKOUT", "ready")
                    marker("WATCHING", "off")
                elif cmd == "stop":
                    pass  # only matters while watching
            except Exception as e:
                if closed.is_set():
                    break
                log(f"Error: {e!r}")
                marker("WATCHING", "off")
        log("Browser closed.")
        marker("BROWSER", "closed")
        try:
            ctx.close()
        except Exception:
            pass


def make_parser():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login", help="open a browser and log in (run also asks you to log in when needed)")
    sub.add_parser("session", help="used by the control panel: one browser, commands on stdin")
    r = sub.add_parser("run", help="poll and add matching slots to the cart")
    r.add_argument("--weeks", type=int, default=4, help="how many weeks ahead to check (default 4)")
    r.add_argument("--time", type=parse_time, help="only add slots starting at this time, e.g. 6am or 6:00am")
    r.add_argument("--start", help="optional first date, YYYY-MM-DD (default: today)")
    r.add_argument("--end", help="optional last date, YYYY-MM-DD (default: --weeks ahead)")
    r.add_argument("--days", default="wed,fri", help="weekdays to check (default: wed,fri)")
    r.add_argument("--keywords", "--title", dest="keywords", type=parse_keywords, default=parse_keywords(DEFAULT_KEYWORDS),
                   help="words the slot must contain, comma separated; capitals, spaces and dashes don't matter"
                        f" (default: {DEFAULT_KEYWORDS!r})")
    r.add_argument("--exclude", default="Goalie",
                   type=lambda v: [w.strip() for w in v.split(",") if w.strip()],
                   help="never add slots containing any of these words, comma separated (default: Goalie)")
    r.add_argument("--participant", help="name to tick if the site asks who is registering")
    r.add_argument("--interval", type=float, default=10, help="seconds between reloads while waiting for a date (default 10)")
    r.add_argument("--headless", action="store_true", help="hide the browser window (you can't check out from a hidden window)")
    r.add_argument("--dry-run", action="store_true", help="report availability but don't click anything")
    r.add_argument("--once", action="store_true", help="do a single round and exit")
    r.add_argument("-v", "--verbose", action="store_true")
    return ap


def main():
    args = make_parser().parse_args()
    {"login": cmd_login, "session": cmd_session, "run": cmd_run}[args.cmd](args)


if __name__ == "__main__":
    main()
