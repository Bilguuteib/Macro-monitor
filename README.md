# Macro Forces Monitor

Tracks three forces that drive risk-asset bull markets, each scored from FRED:
Force A monetary rescue, Force B liquidity & fiscal flood, Force C growth & innovation.
Plus a recession radar. Includes a date picker (monthly history ~1960 to now) and
a hidden research page with the full PDF and summary. Emails when the number of
active forces changes.

Free: FRED API, GitHub Actions, GitHub Pages.

## Data honesty
Real yields start 2003, credit spreads ~1996, yield curve 1976. Earlier months
render with those signals marked "no data", so pre-2003 readings are partial.
Edit HISTORY_START in monitor.py to change the earliest month.

## Setup
1. Free FRED key: https://fredaccount.stlouisfed.org/apikeys
2. Private GitHub repo, push these files (including research.pdf).
3. Settings > Secrets and variables > Actions > add `FRED_API_KEY`.
4. Email alerts (optional): add `GMAIL_USER`, `GMAIL_APP_PASSWORD` (Gmail app password, needs 2FA), `ALERT_TO`.
5. Actions > Macro Monitor > Run workflow. Generates history.json (may take a minute).
6. Settings > Pages > deploy from main, root. That URL is your dashboard; add to phone home screen.

Runs daily at 12:30 UTC, rebuilds history, emails only on an active-force change.

## Files
- monitor.py     builds monthly history.json, handles email
- index.html     dashboard (date picker + research page)
- research.pdf   the full research document
- .github/workflows/monitor.yml  daily schedule
- manifest.json  PWA install
- preview.html   self-contained local preview (open by double-click)
