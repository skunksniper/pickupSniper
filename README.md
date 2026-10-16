# Pickup Sniper

Grabs Sharks Ice **Drop-In Hockey** slots the moment they're released and puts them in your cart.
It never pays. You check out yourself.

## First time on a computer

You need a Mac (or Windows PC) with Python 3. Open **Terminal** and paste these one line at a time:

```zsh
cd ~
git clone -b claude/confident-gates-hufqz6 https://github.com/skunksniper/pickupSniper.git
cd pickupSniper
python3 app.py
```

If your Mac pops up asking to install "developer tools", click **Install**, wait, then run the lines again.
On Windows, use `py app.py` instead of `python3 app.py`.

A control panel opens in your web browser. Click **Install** once (takes about a minute).

## Every time after that

```zsh
cd ~/pickupSniper
python3 app.py
```

It updates itself to the newest version each time it starts. Keep the Terminal window open while you use it.

## Using the panel

1. **Open browser.** A separate browser window opens on the DaySmart site. Log in there.
2. **I'm logged in.** Click it back in the panel and wait for **✓ logged in**.
3. **Choose what to grab:**
   - **Days:** which weekdays (Wednesday and Friday are preselected).
   - **Start with date:** the next date that's about to be released.
   - **Start time:** for example 6:00 AM, or *Any time*.
   - **Never add slots containing:** `Goalie` is already there, so goalie slots are always skipped.
4. **Start watching.** It waits on your start date, reloading that page every few seconds until the slots
   come out. Then it adds the open ones and moves straight on to the next date.
5. **Check out.** When it has something, it clicks through Select Registrants and Next Registrant for you
   and stops on the checkout page. The panel says **Stopped: reached checkout**. Pay in that same browser
   window, not your usual browser, because that's where your cart is.

**Stop** pauses watching without closing the browser. **Close browser** when you're done.

## Tips

- Try **Test mode** first: it finds slots and shows them in the log but doesn't add anything.
- Tick **Detailed log** to see why each slot was skipped.
- If it says **not logged in**, log in again in the browser window, then click **I'm logged in**.
- A yellow "already registered" page means you're signed up for that session already. Check the total
  before paying so you don't pay twice.
- Your login is saved in the `.browser-profile` folder. Don't share that folder with anyone.

## Command line (optional)

Everything the panel does also works from Terminal:

```zsh
python3 sniper.py run --start 2026-10-21 --time 6am         # watch from Oct 21, 6:00 AM slots only
python3 sniper.py run --dry-run --once -v                   # look once without adding anything
python3 sniper.py run --help                                # all options
```

Useful options: `--days wed,fri`, `--time 6am`, `--exclude Goalie`, `--keywords "Drop-In Hockey"`,
`--start YYYY-MM-DD`, `--weeks 4`, `--interval 10` (seconds between reloads).
