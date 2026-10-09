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
import random
import re
import sys
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
STATE_FILE = HERE / "secured.json"
SHOTS_DIR = HERE / "shots"
DEBUG_DIR = HERE / "debug"

# Button text that means "I can book this"
BOOK_RE = re.compile(r"^\s*(register|add to cart|book|sign up|select|reserve)\b", re.I)
# Text on a card that means it's not bookable right now
UNAVAILABLE_RE = re.compile(r"sold out|\bfull\b|wait ?list|not (yet )?available|registration (opens|closed)", re.I)
# Buttons that move a follow-up dialog forward
CONFIRM_RE = re.compile(r"^\s*(add to cart|add|continue|next|confirm|register|save)\b", re.I)
SUCCESS_RE = re.compile(r"added to (your )?cart|item added|in your cart", re.I)


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


def load_state():
    try:
        return set(json.loads(STATE_FILE.read_text()))
    except (FileNotFoundError, ValueError):
        return set()


def save_state(secured):
    STATE_FILE.write_text(json.dumps(sorted(secured), indent=2))


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
    return p.chromium.launch_persistent_context(
        str(PROFILE_DIR), headless=headless, viewport={"width": 1280, "height": 1000}
    )


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


def cmd_login(_args):
    with sync_playwright() as p:
        ctx = open_context(p, headless=False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(f"{BASE_URL}?{QUERY}")
        input("Log in to your DaySmart account in the browser window, then press Enter here... ")
        ctx.close()
    log(f"Session saved to {PROFILE_DIR}")


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


def finish_dialog(page, participant, exclude=()):
    """Click through whatever comes after the first Register click until the item is in the cart."""
    for _ in range(5):
        page.wait_for_timeout(1200)
        if SUCCESS_RE.search(page.inner_text("body")):
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
    return bool(SUCCESS_RE.search(page.inner_text("body"))) or "cart" in page.url.lower()


LAST_SUMMARY = {}


def fmt_time(t):
    return f"{t.hour % 12 or 12}:{t.minute:02d} {'AM' if t.hour < 12 else 'PM'}"


def scan_day(page, day, args, secured):
    url = f"{BASE_URL}?date={day.isoformat()}&{QUERY}"
    page.goto(url, wait_until="domcontentloaded")
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except PWTimeout:
        pass
    page.wait_for_timeout(1000)

    if re.search(r"\b(log ?in|sign ?in)\b", page.url, re.I):
        log("Looks like you're logged out. Run `python sniper.py login` again.")
        sys.exit(2)

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
            skip("already added", text)
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
        btn.click()
        ok = finish_dialog(page, args.participant, args.exclude)
        path = shot(page, f"{day}-{'added' if ok else 'check'}")
        if not ok:
            dump(f"{day}-after-click", page.content())
        if ok:
            log(f"  ADDED TO CART. Go check out! (screenshot: {path})")
            print("\a", end="", flush=True)
            secured.add(key)
            save_state(secured)
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
    return added


def open_checkout(ctx, page, cart_tab):
    """Show the cart/checkout in its own tab so polling can keep going in the other one."""
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
    cart_tab.goto(url, wait_until="domcontentloaded")
    try:
        cart_tab.wait_for_load_state("networkidle", timeout=10000)
    except PWTimeout:
        pass
    # Go straight to checkout if the cart has a button for it (this never pays)
    btn = cart_tab.get_by_role("button", name=re.compile(r"check ?out", re.I))
    if btn.count() == 0:
        btn = cart_tab.get_by_role("link", name=re.compile(r"check ?out", re.I))
    try:
        if btn.count() and btn.first.is_enabled():
            btn.first.click()
    except Exception:
        pass
    cart_tab.bring_to_front()
    log(f"  Opened your cart/checkout in a new tab: {cart_tab.url}")
    return cart_tab


def upcoming_days(args, weekdays):
    """Dates to check right now. Recomputed every round so the window rolls forward on its own."""
    today = dt.date.today()
    start = max(today, dt.date.fromisoformat(args.start)) if args.start else today
    end = dt.date.fromisoformat(args.end) if args.end else today + dt.timedelta(weeks=args.weeks)
    return list(daterange(start, end, weekdays))


def cmd_run(args):
    weekdays = parse_weekdays(args.days)
    secured = load_state()
    days = upcoming_days(args, weekdays)
    if not days:
        sys.exit(f"No upcoming {args.days} dates to check")
    log(f"Watching {args.days} for slots containing {' + '.join(args.keywords)}"
        + (f" starting at {fmt_time(args.time)}" if args.time else "")
        + f", every ~{args.interval}s" + (" (DRY RUN)" if args.dry_run else ""))
    log(f"Dates: {', '.join(f'{d:%a %b %d}' for d in days)}")

    with sync_playwright() as p:
        ctx = open_context(p, headless=args.headless)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        rounds = 0
        cart_tab = None
        try:
            while True:
                rounds += 1
                new_days = upcoming_days(args, weekdays)
                if not new_days:
                    log("No dates left to check.")
                    break
                if new_days != days:
                    days = new_days
                    log(f"Dates: {', '.join(f'{d:%a %b %d}' for d in days)}")
                for day in days:
                    try:
                        if scan_day(page, day, args, secured):
                            cart_tab = open_checkout(ctx, page, cart_tab)
                    except PWTimeout:
                        log(f"{day}: page timed out, will retry")
                    except Exception as e:
                        log(f"{day}: error {e!r}")
                if args.once:
                    break
                if cart_tab is not None and not args.keep_going:
                    log("Got slot(s) this round. Stopping so you can check out.")
                    break
                if rounds % 20 == 0:
                    log(f"still watching ({rounds} rounds, {len(secured)} added so far)")
                time.sleep(args.interval + random.uniform(0, args.interval * 0.3))
        except KeyboardInterrupt:
            log("stopped")
        finally:
            if cart_tab is not None and not args.headless:
                print("\a", end="", flush=True)
                try:
                    input("Slots are in your cart. Finish checkout in the browser, then press Enter here to close it... ")
                except (KeyboardInterrupt, EOFError):
                    pass
            ctx.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login", help="open a browser so you can log in once")
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
    r.add_argument("--interval", type=float, default=30, help="seconds between rounds (default 30)")
    r.add_argument("--headless", action="store_true", help="hide the browser window (you can't check out from a hidden window)")
    r.add_argument("--dry-run", action="store_true", help="report availability but don't click anything")
    r.add_argument("--keep-going", action="store_true", help="keep polling after adding slots instead of stopping")
    r.add_argument("--once", action="store_true", help="do a single round and exit")
    r.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    {"login": cmd_login, "run": cmd_run}[args.cmd](args)


if __name__ == "__main__":
    main()
