# pickupSniper

Watches Sharks Ice (DaySmart) event registration for **"OIC - Drop-In Hockey"** slots and adds
them to your cart the moment they open. It never checks out; you still pay yourself.

## Easiest: the control panel

If this folder is a git checkout, `app.py` pulls the latest version every time it starts.

```bash
python3 app.py
```

It opens in your web browser. Click **Install** and **Open login window** once, then pick your
days and start time and click **Start watching**. When slots land in your cart, a checkout tab
opens. Finish paying there, then click **Done** in the panel.

## Command line

### Setup (once)

```bash
pip install playwright
python -m playwright install chromium
python sniper.py login        # a browser opens: log in, then press Enter in the terminal
```

Your login is kept in `.browser-profile/` (git-ignored). Don't share that folder.

### Run

```bash
# Upcoming Wednesdays and Fridays, 4 weeks ahead (the window rolls forward by itself):
python sniper.py run

# Only slots that start at 6:00 AM:
python sniper.py run --time 6am

# Try it without clicking anything:
python sniper.py run --dry-run --once -v
```

Options:
- `--time 6am` only adds slots starting at that time.
- `--exclude Goalie` never adds slots containing these words (comma separated; default Goalie).
- `--days wed,fri` sets which weekdays to check (default Wednesday and Friday).
- `--weeks 4` sets how far ahead to look; `--start`/`--end` pin exact dates instead.
- `--interval 30` sets the seconds between polling rounds (a random 0–30% is added).
- `--participant NAME` is the name to tick if the site asks who is registering.
- `--keywords "Drop-In Hockey"` sets the words a slot must contain (comma separated; capitals, spaces and dashes are ignored).
- `--headless` hides the browser window.

The script only remembers what it added during the current run, so a fresh start always looks again. Screenshots of each
add attempt go to `shots/`. Stop it with Ctrl+C.
