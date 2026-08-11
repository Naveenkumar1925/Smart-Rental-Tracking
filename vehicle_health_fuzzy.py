
import argparse
import csv
import datetime as dt
import os
import random
import zlib


def _stable_seed(text):
    """Deterministic 32-bit seed from a string (Python's hash() is salted per run)."""
    return zlib.crc32(text.encode()) & 0xFFFFFFFF


BASELINE_DAYS = 7
RECENT_DAYS = 7
HISTORY_DAYS = 30

FUEL_RISE_WARN = 0.15
FUEL_RISE_BAD = 0.30
TEMP_WARN_C = 95.0
TEMP_REDLINE_C = 108.0
HIGH_HOURS = 5500.0
FUEL_PRICE_PER_L = 95.0        

STRAIN_PER_UPGRADE = 10
STRAIN_PER_SERVICE = 4
SITE_HEALTHY_MIN = 75
SITE_STRAINED_MIN = 55

DB_DIR = "rental_db"
MASTER_FILE = os.path.join(DB_DIR, "master_fleet.csv")
TELEMETRY_FILE = os.path.join(DB_DIR, "telemetry.csv")

SITES = {
    "S001": {"name": "Ambattur, Chennai",             "kind": "construction"},
    "S002": {"name": "Tambaram, Chennai",             "kind": "construction"},
    "S003": {"name": "Whitefield, Bengaluru",         "kind": "construction"},
    "S004": {"name": "Electronic City, Bengaluru",    "kind": "mining"},
    "S005": {"name": "Yelahanka, Bengaluru",          "kind": "mining"},
    "S006": {"name": "Sholinganallur (OMR), Chennai", "kind": "mining"},
}
REAL_SITES = ["S001", "S002", "S003", "S004", "S005", "S006"]

FALLBACK_FLEET = [
    ("320-U01", "Excavator"), ("320-U03", "Excavator"), ("D5-U02", "Dozer"),
    ("770-U01", "Off-Highway Truck"), ("120-U04", "Grader"),
    ("CB10-U02", "Compactor"), ("MD6200-U01", "Drill"),
]


def mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def clamp(v, lo=0, hi=100):
    return max(lo, min(hi, v))


def slope_per_day(ys):
    """Least-squares slope of ys vs day index. Positive = rising."""
    n = len(ys)
    if n < 2:
        return 0.0
    xs = list(range(n))
    mx, my = mean(xs), mean(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    den = sum((x - mx) ** 2 for x in xs)
    return num / den if den else 0.0


def pct_change(baseline, recent):
    if baseline <= 1e-9:
        return 0.0
    return (recent - baseline) / baseline
def load_fleet():
    """[(product_id, type), ...] from master_fleet.csv, else the fallback list."""
    if os.path.exists(MASTER_FILE):
        fleet = []
        with open(MASTER_FILE, newline="") as f:
            for row in csv.DictReader(f):
                fleet.append((row["product_id"], row.get("type", "Machine")))
        if fleet:
            return fleet
    return list(FALLBACK_FLEET)


def load_master_sites():
    """{product_id: home_site} from master_fleet.csv, if it has that column."""
    out = {}
    if os.path.exists(MASTER_FILE):
        with open(MASTER_FILE, newline="") as f:
            for row in csv.DictReader(f):
                site = row.get("home_site", "")
                if site:
                    out[row["product_id"]] = site
    return out


def assign_sites(fleet):
   
    master = load_master_sites()
    ids = sorted((pid for pid, _ in fleet), key=_stable_seed)
    out = {}
    rr = 0
    real_count = 0
    for pid in ids:
        home = master.get(pid)
        if home in SITES:
            out[pid] = home
            real_count += 1
        elif home == "NULL":
            out[pid] = "NULL"
        else:                                   # missing / unknown -> real site
            out[pid] = REAL_SITES[rr % len(REAL_SITES)]
            rr += 1
            real_count += 1

    if real_count == 0:                         # all yard -> spread so board works
        for i, pid in enumerate(ids):
            out[pid] = REAL_SITES[i % len(REAL_SITES)]
    return out


def assign_profiles(fleet):
    """
    Hidden wear story per machine, spread by QUOTA (not chance) so a demo always
    shows over-killed, aging AND healthy machines. Reproducible via stable seed.
    """
    ids = sorted((pid for pid, _ in fleet), key=_stable_seed)
    n = len(ids)
    n_over = max(1, round(n * 0.15))
    n_age = max(1, round(n * 0.30))
    profiles = {}
    for i, pid in enumerate(ids):
        if i < n_over:
            profiles[pid] = "overkilled"
        elif i < n_over + n_age:
            profiles[pid] = "aging"
        else:
            profiles[pid] = "healthy"
    return profiles


# --------------------------------------------------------------------------- #
#  Fake telemetry (now carries site_id)
# --------------------------------------------------------------------------- #
def build_fake_telemetry():
    os.makedirs(DB_DIR, exist_ok=True)
    fleet = load_fleet()
    sites = assign_sites(fleet)
    profiles = assign_profiles(fleet)
    start = dt.date.today() - dt.timedelta(days=HISTORY_DAYS - 1)

    rows = []
    for pid, _type in fleet:
        profile = profiles[pid]
        site = sites[pid]
        random.seed(_stable_seed(pid + "_tel"))

        base_fuel_rate = random.uniform(9.0, 16.0)
        base_temp = random.uniform(82.0, 90.0)
        cum_hours = random.uniform(1500, 6500)

        if profile == "healthy":
            fuel_drift, temp_drift, noise = 0.03, 0.05, 0.6
        elif profile == "aging":
            fuel_drift, temp_drift, noise = 0.18, 0.35, 0.9
        else:  # overkilled
            fuel_drift, temp_drift, noise = 0.45, 0.95, 1.2

        for d in range(HISTORY_DAYS):
            frac = d / max(1, HISTORY_DAYS - 1)
            engine_hrs = max(0.5, random.gauss(7.5, 1.5))
            cum_hours += engine_hrs

            fuel_rate = base_fuel_rate * (1 + fuel_drift * frac) \
                + random.uniform(-noise, noise) * 0.1
            fuel_l = round(fuel_rate * engine_hrs, 1)

            temp = base_temp + (TEMP_REDLINE_C - base_temp) * temp_drift * frac \
                + random.uniform(-noise, noise)
            temp = round(temp, 1)

            rows.append({
                "product_id": pid, "site_id": site,
                "date": (start + dt.timedelta(days=d)).isoformat(),
                "engine_hrs": round(engine_hrs, 1), "fuel_l": fuel_l,
                "engine_temp_c": temp, "_cum_hours": round(cum_hours, 1),
            })

    with open(TELEMETRY_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["product_id", "site_id", "date",
                                          "engine_hrs", "fuel_l", "engine_temp_c",
                                          "_cum_hours"])
        w.writeheader()
        w.writerows(rows)
    print(f"[db] wrote {len(rows)} telemetry rows for {len(fleet)} machines "
          f"across {len(set(sites.values()))} sites -> ./{TELEMETRY_FILE}")


def _telemetry_is_stale():
    """True if telemetry.csv predates the site_id column (old schema)."""
    try:
        with open(TELEMETRY_FILE) as f:
            header = f.readline()
        return "site_id" not in [c.strip() for c in header.split(",")]
    except OSError:
        return False


def ensure_db(reset=False):
    if reset and os.path.exists(TELEMETRY_FILE):
        os.remove(TELEMETRY_FILE)
    if not os.path.exists(TELEMETRY_FILE):
        build_fake_telemetry()
    elif _telemetry_is_stale():
        print("[db] telemetry.csv has no site_id column (old version) "
              "-> rebuilding with sites.")
        os.remove(TELEMETRY_FILE)
        build_fake_telemetry()


def load_telemetry():
    """{product_id: [row, ...]} sorted by date; numbers parsed; site_id kept."""
    series = {}
    with open(TELEMETRY_FILE, newline="") as f:
        for row in csv.DictReader(f):
            row["engine_hrs"] = float(row["engine_hrs"])
            row["fuel_l"] = float(row["fuel_l"])
            row["engine_temp_c"] = float(row["engine_temp_c"])
            row["_cum_hours"] = float(row.get("_cum_hours", 0) or 0)
            row["site_id"] = row.get("site_id", "NULL")
            series.setdefault(row["product_id"], []).append(row)
    for pid in series:
        series[pid].sort(key=lambda r: r["date"])
    return series


# --------------------------------------------------------------------------- #
#  Machine health analysis
# --------------------------------------------------------------------------- #
def analyse(pid, rows, type_):
    fuel_rate = [r["fuel_l"] / r["engine_hrs"] if r["engine_hrs"] else 0.0 for r in rows]
    temp = [r["engine_temp_c"] for r in rows]

    base_fr = mean(fuel_rate[:BASELINE_DAYS])
    recent_fr = mean(fuel_rate[-RECENT_DAYS:])
    fr_rise = pct_change(base_fr, recent_fr)

    recent_temp = mean(temp[-RECENT_DAYS:])
    max_temp = max(temp)
    cum_hours = rows[-1]["_cum_hours"]

    fr_trend = slope_per_day(fuel_rate)
    temp_trend = slope_per_day(temp)

    penalties = []
    score = 100
    if fr_rise >= FUEL_RISE_BAD:
        score -= 32
        penalties.append(f"fuel burn +{fr_rise*100:.0f}% vs baseline (heavy)")
    elif fr_rise >= FUEL_RISE_WARN:
        score -= 18
        penalties.append(f"fuel burn +{fr_rise*100:.0f}% vs baseline")
    if recent_temp >= TEMP_REDLINE_C:
        score -= 35
        penalties.append(f"engine at {recent_temp:.0f} C (near redline)")
    elif recent_temp > TEMP_WARN_C:
        score -= 20
        penalties.append(f"engine running hot at {recent_temp:.0f} C")
    if fr_trend > 0.02:
        score -= 12
        penalties.append("fuel efficiency trending worse")
    if temp_trend > 0.15:
        score -= 12
        penalties.append("temperature trending up")
    if cum_hours > HIGH_HOURS:
        score -= 10
        penalties.append(f"high mileage ({cum_hours:.0f} engine hrs)")
    score = clamp(score)

    if score >= 70:
        verdict = "HEALTHY"
    elif score >= 45:
        verdict = "SERVICE SOON"
    else:
        verdict = "UPGRADE RECOMMENDED"

    typical_hrs = mean([r["engine_hrs"] for r in rows[-RECENT_DAYS:]])
    extra_l_per_day = max(0.0, (recent_fr - base_fr)) * typical_hrs
    wasted_inr_day = extra_l_per_day * FUEL_PRICE_PER_L

    return {
        "pid": pid, "type": type_, "site": rows[-1]["site_id"],
        "score": score, "verdict": verdict,
        "base_fr": base_fr, "recent_fr": recent_fr, "fr_rise": fr_rise,
        "recent_temp": recent_temp, "max_temp": max_temp,
        "fr_trend": fr_trend, "temp_trend": temp_trend, "cum_hours": cum_hours,
        "penalties": penalties, "wasted_inr_day": wasted_inr_day,
        "extra_l_day": extra_l_per_day, "rows": rows,
    }


# --------------------------------------------------------------------------- #
#  Site scoring (rolls machine scores up per site)
# --------------------------------------------------------------------------- #
def score_sites(results):
    """Return [site_summary, ...] sorted worst-first. Yard/NULL excluded."""
    by_site = {}
    for r in results:
        if r["site"] not in SITES:          # skip NULL / yard
            continue
        by_site.setdefault(r["site"], []).append(r)

    summaries = []
    for site, rs in by_site.items():
        n_up = sum(1 for r in rs if r["verdict"] == "UPGRADE RECOMMENDED")
        n_sv = sum(1 for r in rs if r["verdict"] == "SERVICE SOON")
        raw = mean([r["score"] for r in rs])
        strain = STRAIN_PER_UPGRADE * n_up + STRAIN_PER_SERVICE * n_sv
        site_score = int(round(clamp(raw - strain)))

        if site_score < SITE_STRAINED_MIN or n_up > 0:
            status = "OVER-WORKED"
        elif site_score < SITE_HEALTHY_MIN:
            status = "STRAINED"
        else:
            status = "HEALTHY"

        worst = min(rs, key=lambda r: r["score"])
        summaries.append({
            "site": site, "name": SITES[site]["name"], "kind": SITES[site]["kind"],
            "score": site_score, "status": status, "n": len(rs),
            "n_up": n_up, "n_sv": n_sv, "raw": raw,
            "bleed": sum(r["wasted_inr_day"] for r in rs),
            "worst": worst, "machines": sorted(rs, key=lambda r: r["score"]),
        })
    return sorted(summaries, key=lambda s: s["score"])


# --------------------------------------------------------------------------- #
#  Reporting
# --------------------------------------------------------------------------- #
def _bar(score, width=20):
    filled = int(round(score / 100 * width))
    return "#" * filled + "." * (width - filled)


def report_machine_detail(r):
    print(f"\n  {r['pid']}  [{r['type']}] @ {r['site']}   {_bar(r['score'])}  "
          f"{r['score']}/100  ->  {r['verdict']}")
    print(f"    fuel burn : {r['base_fr']:.1f} -> {r['recent_fr']:.1f} L/hr "
          f"({r['fr_rise']*100:+.0f}% vs baseline)")
    print(f"    engine temp: recent avg {r['recent_temp']:.0f}C, peak {r['max_temp']:.0f}C "
          f"(trend {r['temp_trend']:+.2f} C/day)")
    print(f"    engine hrs : {r['cum_hours']:.0f} cumulative")
    if r["penalties"]:
        print("    flags      : " + "; ".join(r["penalties"]))
    if r["wasted_inr_day"] > 1:
        print(f"    money bleed: ~{r['extra_l_day']:.1f} L/day = "
              f"Rs.{r['wasted_inr_day']:,.0f}/day over a healthy engine")
    if r["verdict"] == "UPGRADE RECOMMENDED":
        print("    ADVICE     : over-killed - replacing it is now cheaper than the "
              "fuel + downtime it costs.")


def report_fleet(results, upgrades_only=False, site_filter=None):
    results = sorted(results, key=lambda r: r["score"])
    title = "VEHICLE HEALTH / UPGRADE ADVISOR"
    if site_filter:
        title += f"  (site {site_filter})"
    elif upgrades_only:
        title += "  (upgrade candidates only)"
    print("=" * 78)
    print(title)
    print("=" * 78)
    print(f"{'MACHINE':<14}{'SITE':<7}{'TYPE':<18}{'SCORE':<9}{'TEMP':<7}{'FUEL/HR':<9}VERDICT")
    print("-" * 78)
    shown = 0
    for r in results:
        if site_filter and r["site"] != site_filter:
            continue
        if upgrades_only and r["verdict"] != "UPGRADE RECOMMENDED":
            continue
        shown += 1
        print(f"{r['pid']:<14}{r['site']:<7}{r['type'][:16]:<18}"
              f"{r['score']:>3}/100  {r['recent_temp']:>4.0f}C  "
              f"{r['recent_fr']:>5.1f}L/h  {r['verdict']}")
    if shown == 0:
        print("  (nothing in this category)")
    print("-" * 78)
    for r in results:
        if site_filter and r["site"] != site_filter:
            continue
        if upgrades_only and r["verdict"] != "UPGRADE RECOMMENDED":
            continue
        if r["verdict"] == "HEALTHY" and not (upgrades_only or site_filter):
            continue
        report_machine_detail(r)
    print()


def report_sites(summaries):
    print("=" * 78)
    print("SITE SCOREBOARD  -  which sites are over-working their fleet")
    print("=" * 78)
    if not summaries:
        print("  No machines are assigned to a real site (all at NULL/yard).")
        print("  Fix: give master_fleet.csv a home_site of S001..S006, or run")
        print("       python vehicle_health.py --reset")
        print("-" * 78 + "\n")
        return
    print(f"{'SITE':<6}{'LOCATION':<28}{'KIND':<13}{'SCORE':<9}{'MACHINES':<13}STATUS")
    print("-" * 78)
    for s in summaries:
        machines = f"{s['n']} ({s['n_up']}up/{s['n_sv']}sv)"
        print(f"{s['site']:<6}{s['name'][:26]:<28}{s['kind']:<13}"
              f"{s['score']:>3}/100  {machines:<13}{s['status']}")
    print("-" * 78)
    for s in summaries:
        if s["status"] == "HEALTHY":
            continue
        print(f"\n  {s['site']}  {s['name']}   {_bar(s['score'])}  {s['score']}/100  "
              f"->  {s['status']}")
        print(f"    fleet: {s['n']} machines "
              f"({s['n_up']} to upgrade, {s['n_sv']} to service); "
              f"avg machine score {s['raw']:.0f}")
        print(f"    worst unit: {s['worst']['pid']} [{s['worst']['type']}] "
              f"{s['worst']['score']}/100 ({s['worst']['verdict']})")
        if s["bleed"] > 1:
            print(f"    fuel bleed: ~Rs.{s['bleed']:,.0f}/day wasted across this site")
        if s["status"] == "OVER-WORKED":
            print("    ADVICE    : this site is over-killing its equipment - "
                  "rebalance load\n                or borrow an idle machine from a "
                  "nearby site (equipment_sharing.py).")
    print()


def report_one(pid, res):
    print("=" * 78)
    print(f"DAY-BY-DAY  {pid}  [{res['type']}] @ site {res['site']}   "
          f"verdict: {res['verdict']} ({res['score']}/100)")
    print("=" * 78)
    print(f"{'DATE':<12}{'ENG hrs':<9}{'FUEL L':<9}{'L/HR':<8}{'TEMP C':<8}")
    print("-" * 78)
    for r in res["rows"]:
        fr = r["fuel_l"] / r["engine_hrs"] if r["engine_hrs"] else 0
        hot = "  <-- hot" if r["engine_temp_c"] > TEMP_WARN_C else ""
        print(f"{r['date']:<12}{r['engine_hrs']:<9.1f}{r['fuel_l']:<9.1f}"
              f"{fr:<8.1f}{r['engine_temp_c']:<8.1f}{hot}")
    print("-" * 78)
    print(f"  fuel burn {res['base_fr']:.1f} -> {res['recent_fr']:.1f} L/hr "
          f"({res['fr_rise']*100:+.0f}%), temp trend {res['temp_trend']:+.2f} C/day")
    if res["penalties"]:
        print("  flags: " + "; ".join(res["penalties"]))
    print()
def main():
    p = argparse.ArgumentParser(
        description="Over-kill / upgrade advisor + site score for the rental fleet.")
    p.add_argument("--reset", action="store_true", help="rebuild fake telemetry")
    p.add_argument("--sites", action="store_true", help="show only the site scoreboard")
    p.add_argument("--site", metavar="SITE_ID", help="one site: its score + machines")
    p.add_argument("--upgrades", action="store_true",
                   help="show only machines flagged UPGRADE RECOMMENDED")
    p.add_argument("--only", metavar="PRODUCT_ID", help="day-by-day detail for one machine")
    args = p.parse_args()

    ensure_db(reset=args.reset)

    fleet = dict(load_fleet())
    series = load_telemetry()
    results = [analyse(pid, rows, fleet.get(pid, "Machine"))
               for pid, rows in series.items()
               if len(rows) >= BASELINE_DAYS + 1]

    if args.only:
        match = next((r for r in results if r["pid"] == args.only), None)
        if not match:
            print(f"no telemetry for {args.only}. Known ids: {', '.join(sorted(series))}")
            return
        report_one(args.only, match)
        return

    if args.site:
        site = args.site.upper()
        summaries = [s for s in score_sites(results) if s["site"] == site]
        if not summaries:
            print(f"no scored machines at site {site}. "
                  f"Sites with data: {', '.join(sorted({r['site'] for r in results}))}")
            return
        report_sites(summaries)
        report_fleet(results, site_filter=site)
        return

    if args.sites:
        report_sites(score_sites(results))
        return

    if args.upgrades:
        report_fleet(results, upgrades_only=True)
        return
    report_fleet(results)
    report_sites(score_sites(results))
if __name__ == "__main__":
    main()