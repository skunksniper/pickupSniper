# pickupSniper

Watches Sharks Ice (DaySmart) event registration for **"OIC - Drop-In Hockey"** slots and adds
them to your cart the moment they open. It never checks out; you still pay yourself.

## Setup (once)

```bash
pip install playwright
python -m playwright install chromium
python sniper.py login        # a browser opens: log in, then press Enter in the terminal
```

Your login is kept in `.browser-profile/` (git-ignored). Don't share that folder.

## Run

```bash
# Try it first without clicking anything:
python sniper.py run --start 2026-10-10 --end 2026-10-31 --dry-run --once -v

# For real:
python sniper.py run --start 2026-10-10 --end 2026-10-31 --participant "Cassidy"
```

Options:
- `--interval 30` sets the seconds between polling rounds (a random 0–30% is added).
- `--participant NAME` is the name to tick if the site asks who is registering.
- `--title "..."` matches a different event title.
- `--headless` hides the browser window.

Slots already added are saved in `secured.json`, so they won't be added twice. Screenshots of each
add attempt go to `shots/`. Stop it with Ctrl+C.
