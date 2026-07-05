"""
Macro forces monitor with full monthly history (approx 1960 -> now).

Pulls each FRED series once, then computes the three-force snapshot for every
month-end, writing history.json for the dashboard's date picker. Also emails
when the latest month's active-force count changes.

Data reality: real yields (DFII10) start 2003, credit spreads ~1996, the
10y-2y curve 1976. Earlier months render with those signals marked no-data,
so pre-2003 readings are partial by construction. START is adjustable.
"""
import bisect
import json
import os
import smtplib
import ssl
import urllib.request
from datetime import date, datetime, timezone
from email.mime.text import MIMEText

FRED_KEY = os.environ.get("FRED_API_KEY", "")
BASE = "https://api.stlouisfed.org/fred/series/observations"
START = os.environ.get("HISTORY_START", "1960-01")   # first month in history

SERIES = {
    "real_yield": "DFII10", "fed_upper": "DFEDTARU", "fed_eff": "DFF",
    "m2": "M2SL", "fed_bal": "WALCL", "core_pce": "PCEPILFE",
    "hy_spread": "BAMLH0A0HYM2", "deficit": "MTSDS133FMS", "gdp": "GDPC1",
    "unrate": "UNRATE", "claims": "ICSA", "indpro": "INDPRO",
    "sahm": "SAHMREALTIME", "curve": "T10Y2Y", "nasdaq": "NASDAQCOM",
}


def fetch_asc(series_id):
    """Full history ascending as [(date_str, value)], or [] on failure."""
    try:
        url = (f"{BASE}?series_id={series_id}&api_key={FRED_KEY}"
               f"&file_type=json&sort_order=asc&limit=100000")
        with urllib.request.urlopen(url, timeout=60) as r:
            obs = json.load(r)["observations"]
        return [(o["date"], float(o["value"])) for o in obs if o["value"] != "."]
    except Exception as e:
        print(f"  warn: {series_id} failed ({e})")
        return []


# newest-first accessors used by scoring
def val(v, n=0):
    return v[n][1] if v and len(v) > n else None


def yoy(v):
    return (v[0][1] / v[12][1] - 1) * 100 if v and len(v) > 12 else None


def change(v, n):
    a, b = val(v, 0), val(v, n)
    return (a - b) if (a is not None and b is not None) else None


def pct_change(v, n):
    a, b = val(v, 0), val(v, n)
    return ((a / b - 1) * 100) if (a and b) else None


def sig(score, name, value, unit, note):
    return {"name": name,
            "value": round(value, 2) if value is not None else None,
            "unit": unit, "note": note,
            "state": {1: "favorable", 0: "neutral", -1: "unfavorable"}[score],
            "_s": score}


def force_from(subs):
    live = [s for s in subs if s is not None]
    total = sum(s["_s"] for s in live)
    n = len(live)
    if n == 0:
        state = "no data"
    elif total >= max(2, n - 1):
        state = "favorable"
    elif total <= -max(2, n - 1):
        state = "unfavorable"
    else:
        state = "neutral"
    for s in live:
        s.pop("_s", None)
    return {"state": state, "score": total, "max": n, "signals": live}


def force_a(d):
    subs = []
    ry, rc = val(d["real_yield"]), change(d["real_yield"], 63)
    if ry is not None:
        s = 1 if (ry < 1.0 or (rc is not None and rc < -0.25)) else \
            (-1 if (ry > 1.75 and (rc or 0) > 0) else 0)
        subs.append(sig(s, "Real yield falling", ry, "%",
                    "low/falling" if s == 1 else "high/rising" if s == -1
                    else "middling"))
    # prefer target-upper; fall back to effective rate pre-2008
    fed_series = d["fed_upper"] if d["fed_upper"] else d["fed_eff"]
    fu, fc = val(fed_series), change(fed_series, 130)
    if fu is not None:
        s = 1 if (fc is not None and fc <= -0.25) else \
            (-1 if (fc is not None and fc >= 0.25) or fu > 3.0 else 0)
        subs.append(sig(s, "Fed cutting", fu, "%",
                    "cutting" if s == 1 else "hiking/holding high" if s == -1
                    else "neutral"))
    m2y, balc = yoy(d["m2"]), pct_change(d["fed_bal"], 13)
    if m2y is not None:
        s = 1 if (m2y > 5 or (balc is not None and balc > 0.5)) else \
            (-1 if (m2y < 0 or (balc is not None and balc < -0.5)) else 0)
        subs.append(sig(s, "Liquidity (QE/M2)", m2y, "% M2 YoY",
                    "expanding" if s == 1 else "contracting" if s == -1
                    else "flat"))
    cpce = yoy(d["core_pce"])
    if cpce is not None:
        s = 1 if cpce < 2.5 else (-1 if cpce > 3.0 else 0)
        subs.append(sig(s, "Inflation low enough", cpce, "% core PCE",
                    "at/below target" if s == 1 else "too hot" if s == -1
                    else "near target"))
    return force_from(subs)


def force_b(d):
    subs = []
    m2y = yoy(d["m2"])
    m2_prev = None
    if d["m2"] and len(d["m2"]) > 15:
        m2_prev = (d["m2"][3][1] / d["m2"][15][1] - 1) * 100
    if m2y is not None:
        rising = (m2_prev is not None and m2y > m2_prev)
        s = 1 if (m2y > 3 and rising) else (-1 if m2y < 0 else 0)
        subs.append(sig(s, "M2 turning up", m2y, "% YoY",
                    "growing & rising" if s == 1 else "contracting" if s == -1
                    else "soft"))
    balc = pct_change(d["fed_bal"], 13)
    if balc is not None:
        s = 1 if balc > 0.2 else (-1 if balc < -0.5 else 0)
        subs.append(sig(s, "Balance sheet", balc, "% 3m",
                    "expanding" if s == 1 else "QT shrinking" if s == -1
                    else "flat"))
    dv = d["deficit"]
    if dv and len(dv) >= 24:
        recent = sum(x[1] for x in dv[:12])
        prior = sum(x[1] for x in dv[12:24])
        widening = recent < prior
        in_recession = (val(d["sahm"]) or 0) >= 0.5
        if widening and in_recession:
            s, note = 0, "widening, but recession-driven"
        elif widening:
            s, note = 1, "widening (injects)"
        else:
            s, note = -1, "narrowing"
        subs.append(sig(s, "Fiscal deficit", recent / 1000, "$B 12m", note))
    hy = val(d["hy_spread"])
    if hy is not None:
        s = 1 if hy < 3.5 else (-1 if hy > 5.0 else 0)
        subs.append(sig(s, "Spreads calm", hy, "%",
                    "calm" if s == 1 else "stressed" if s == -1
                    else "elevated"))
    return force_from(subs)


def force_c(d):
    subs = []
    g = None
    if d["gdp"] and len(d["gdp"]) > 4:
        g = (d["gdp"][0][1] / d["gdp"][4][1] - 1) * 100
    if g is not None:
        s = 1 if g > 2 else (-1 if g < 0 else 0)
        subs.append(sig(s, "GDP growth", g, "% YoY",
                    "strong" if s == 1 else "contracting" if s == -1
                    else "sluggish"))
    ur, urc = val(d["unrate"]), change(d["unrate"], 6)
    if ur is not None:
        s = 1 if (urc is not None and urc < 0) else \
            (-1 if (urc is not None and urc > 0.3) else 0)
        subs.append(sig(s, "Unemployment", ur, "%",
                    "falling" if s == 1 else "rising" if s == -1 else "flat"))
    cl, clc = val(d["claims"]), pct_change(d["claims"], 13)
    if cl is not None:
        s = 1 if (clc is not None and clc < 5) else \
            (-1 if (clc is not None and clc > 15) else 0)
        subs.append(sig(s, "Jobless claims", cl / 1000, "k",
                    "low/stable" if s == 1 else "rising fast" if s == -1
                    else "drifting"))
    ip = yoy(d["indpro"])
    if ip is not None:
        s = 1 if ip > 1 else (-1 if ip < -1 else 0)
        subs.append(sig(s, "Industrial output", ip, "% YoY",
                    "growing" if s == 1 else "contracting" if s == -1
                    else "flat"))
    return force_from(subs)


def snapshot(d, label):
    A, B, C = force_a(d), force_b(d), force_c(d)
    active = sum(1 for f in (A, B, C) if f["state"] == "favorable")
    sahm, curve = val(d["sahm"]), val(d["curve"])
    hy, hyp = val(d["hy_spread"]), val(d["hy_spread"], 21)
    hyw = (hy is not None and hyp is not None and hy - hyp > 0.5)
    return {
        "as_of": label, "forces_active": active,
        "forces": {"A": {"title": "Monetary rescue", **A},
                   "B": {"title": "Liquidity & fiscal flood", **B},
                   "C": {"title": "Growth & innovation (proxy)", **C}},
        "radar": [
            {"name": "Sahm rule", "value": round(sahm, 2)
             if sahm is not None else None, "unit": "",
             "alert": sahm is not None and sahm >= 0.5,
             "note": "recession trigger hit" if (sahm and sahm >= 0.5)
             else "no recession signal"},
            {"name": "10y-2y curve", "value": round(curve, 2)
             if curve is not None else None, "unit": "%",
             "alert": curve is not None and curve < 0,
             "note": "inverted" if (curve is not None and curve < 0)
             else "normal"},
            {"name": "HY credit spread", "value": round(hy, 2)
             if hy is not None else None, "unit": "%", "alert": hyw,
             "note": "widening fast" if hyw else "calm"},
        ],
    }


def month_ends(start_ym):
    y, m = int(start_ym[:4]), int(start_ym[5:7])
    today = date.today()
    out = []
    while (y, m) <= (today.year, today.month):
        # last day of month = day before first of next month
        nm_y, nm_m = (y + 1, 1) if m == 12 else (y, m + 1)
        last = (date(nm_y, nm_m, 1) - date.resolution).isoformat()
        out.append((f"{y:04d}-{m:02d}", last))
        y, m = nm_y, nm_m
    return out


def slice_asof(series_asc, cutoff_iso):
    """Return observations <= cutoff, newest-first."""
    keys = [d for d, _ in series_asc]
    k = bisect.bisect_right(keys, cutoff_iso)
    return list(reversed(series_asc[:k]))


def add_trends(snaps, asc_order):
    """Per force: score vs 3 months earlier -> improving/worsening/flat."""
    for i, ym in enumerate(asc_order):
        for k in ("A", "B", "C"):
            cur = snaps[ym]["forces"][k]
            if i >= 3:
                prev = snaps[asc_order[i - 3]]["forces"][k]["score"]
                diff = cur["score"] - prev
                cur["trend"] = ("improving" if diff > 0 else
                                "worsening" if diff < 0 else "flat")
            else:
                cur["trend"] = "flat"


def add_validation(hist, nasdaq_asc):
    """Forward 12m NASDAQ return grouped by active-force count.
    NASDAQ starts 1971, so validation covers 1971+ months only."""
    if not nasdaq_asc:
        hist["validation"] = None
        return
    dates = [d for d, _ in nasdaq_asc]
    import bisect as _b

    def px_asof(iso):
        k = _b.bisect_right(dates, iso)
        return nasdaq_asc[k - 1][1] if k else None

    buckets = {0: [], 1: [], 2: [], 3: []}
    asc = list(reversed(hist["months"]))
    for ym in asc:
        y, m = int(ym[:4]), int(ym[5:7])
        end = f"{y:04d}-{m:02d}-28"
        fy, fm = (y + 1, m)
        fend = f"{fy:04d}-{fm:02d}-28"
        p0, p1 = px_asof(end), px_asof(fend)
        # require a full forward year of data
        if p0 and p1 and fend <= dates[-1]:
            n = hist["snapshots"][ym]["forces_active"]
            r = (p1 / p0 - 1) * 100
            buckets[n].append(r)
            hist["snapshots"][ym]["fwd12"] = round(r, 1)
    out = {}
    for n, rets in buckets.items():
        if rets:
            rets.sort()
            out[str(n)] = {
                "months": len(rets),
                "avg_fwd12": round(sum(rets) / len(rets), 1),
                "median_fwd12": round(rets[len(rets) // 2], 1),
                "pct_positive": round(
                    100 * sum(1 for r in rets if r > 0) / len(rets)),
            }
    hist["validation"] = {"asset": "NASDAQ Composite (1971+)",
                          "by_active": out}


# Major US equity bull-market start months (post-1950). The report card
# shows what each force read in that exact month, from real data only.
BULL_RUNS = [
    ("1949-06", "Post-war bull"), ("1957-10", "1957 recovery"),
    ("1962-06", "1962 recovery"), ("1966-10", "1966 recovery"),
    ("1970-05", "1970 recovery"), ("1974-10", "1974 bottom"),
    ("1982-08", "Volcker pivot"), ("1987-12", "Post-crash"),
    ("1990-10", "Gulf-war bottom"), ("2002-10", "Dotcom bottom"),
    ("2009-03", "GFC bottom"), ("2020-03", "COVID bottom"),
    ("2022-10", "2022 bottom"),
]


def add_report_card(hist):
    """For each bull-run start month, capture what each force read then,
    plus trends and 12m-forward return if available."""
    card = []
    for ym, label in BULL_RUNS:
        snap = hist["snapshots"].get(ym)
        if not snap:
            card.append({"month": ym, "label": label, "coverage": "no data"})
            continue
        entry = {"month": ym, "label": label,
                 "active": snap["forces_active"],
                 "fwd12": snap.get("fwd12"),
                 "forces": {}}
        n_signals = 0
        for k in ("A", "B", "C"):
            f = snap["forces"][k]
            entry["forces"][k] = {"state": f["state"],
                                  "trend": f.get("trend", "flat"),
                                  "score": f["score"], "max": f["max"]}
            n_signals += f["max"]
        entry["coverage"] = ("full" if n_signals >= 11 else
                             "partial" if n_signals >= 6 else "thin")
        card.append(entry)
    hist["report_card"] = card


def build_history():
    print(f"Fetching full history (start {START})...")
    full = {k: fetch_asc(v) for k, v in SERIES.items()}
    months = month_ends(START)
    snaps, asc_order = {}, []
    for ym, last in months:
        d = {k: slice_asof(full[k], last) for k in SERIES}
        if not any(d[k] for k in ("m2", "unrate", "gdp")):
            continue
        snaps[ym] = snapshot(d, ym)
        asc_order.append(ym)
    add_trends(snaps, asc_order)
    order = list(reversed(asc_order))
    latest = order[0] if order else None
    hist = {"generated": datetime.now(timezone.utc)
            .strftime("%Y-%m-%d %H:%M UTC"),
            "latest": latest, "months": order, "snapshots": snaps}
    add_validation(hist, full.get("nasdaq", []))
    add_report_card(hist)
    return hist


def load_prior(path="history.json"):
    try:
        with open(path) as f:
            h = json.load(f)
        return h["snapshots"][h["latest"]]["forces_active"]
    except Exception:
        return None


def send_email(old, new, snap):
    user = os.environ.get("GMAIL_USER")
    pw = os.environ.get("GMAIL_APP_PASSWORD")
    to = os.environ.get("ALERT_TO")
    if not (user and pw and to):
        print("Email creds not set, skipping")
        return
    lines = [f"Active forces changed: {old} -> {new} of 3", "",
             f"As of {snap['as_of']}", ""]
    for k in ("A", "B", "C"):
        f = snap["forces"][k]
        lines.append(f"Force {k} - {f['title']}: {f['state']} "
                     f"({f['score']}/{f['max']})")
    msg = MIMEText("\n".join(lines))
    msg["Subject"] = f"Macro forces: {new} of 3 active"
    msg["From"], msg["To"] = user, to
    with smtplib.SMTP_SSL("smtp.gmail.com", 465,
                          context=ssl.create_default_context()) as s:
        s.login(user, pw)
        s.sendmail(user, [to], msg.as_string())
    print(f"Email sent: {old} -> {new}")


if __name__ == "__main__":
    prior = load_prior()
    hist = build_history()
    with open("history.json", "w") as f:
        json.dump(hist, f, separators=(",", ":"))
    latest_snap = hist["snapshots"][hist["latest"]]
    print(f"months: {len(hist['months'])}, latest {hist['latest']}, "
          f"active {latest_snap['forces_active']} (prior {prior})")
    if prior is not None and prior != latest_snap["forces_active"]:
        send_email(prior, latest_snap["forces_active"], latest_snap)
    else:
        print("No change in active-force count, no email")
