import argparse
import datetime as dt
import random
import signal
import sys
import time

SHIFTS_PER_DAY = 3

SITE_BASE = {
    "S001": (13.1143, 80.1548),   # Ambattur, Chennai
    "S002": (12.9249, 80.1000),   # Tambaram, Chennai
    "S006": (12.9010, 80.2279),   # Sholinganallur (OMR), Chennai
    "S003": (12.9698, 77.7500),   # Whitefield, Bengaluru
    "S004": (12.8452, 77.6602),   # Electronic City, Bengaluru
    "S005": (13.1007, 77.5963),   # Yelahanka, Bengaluru
    "NULL": (13.0827, 80.2707),   # yard / unassigned (central Chennai)
}
REAL_SITES = ["S001", "S002", "S003", "S004", "S005", "S006"]

CATALOG = {
    "Skid Steer Loader": ["216B3", "239D3"],
    "Compactor":         ["815", "816", "CB10", "CS19"],
    "Dozer":             ["D3", "D5", "D11"],
    "Dragline":          ["8000", "8200", "8750"],
    "Drill":             ["MD6200", "MD6250", "MD6310", "MD6640"],
    "Excavator":         ["300-9D", "313-GC", "320", "330-UHD", "336", "340-Long-Reach"],
    "Grader":            ["18", "120"],
    "Off-Highway Truck": ["770-(07)", "777-(07)-Water-Truck", "785"],
}

BOUNDS = {
    "engine_temp":  (60.0, 105.0),
    "battery_pct":  (10.0, 100.0),
    "battery_temp": (20.0, 55.0),
    "fuel_level":   (5.0, 100.0),
}

HEADER = [
    "Equipment ID", "Equipment Type", "Site ID", "Check In Date", "Check Out Date",
    "Engine Hrs/Day", "Idle Hours/Day", "Rental Days", "Last Operator ID",
    "Latitude", "Longitude", "Engine Temperature", "Battery Percentage",
    "Battery Temperature", "Fuel Level", "Timestamp",
]

UNALLOCATED_EXTRA_PROB = 0.5


def clamp(value, key):
    lo, hi = BOUNDS[key]
    return max(lo, min(hi, value))


def model_code(model):
    """Filesystem-safe model token for the Equipment ID."""
    return model.replace(" ", "").replace("(", "").replace(")", "")


def random_operator():
    return f"OP{random.randint(100, 399)}"


def three_operators():
    ops = []
    while len(ops) < SHIFTS_PER_DAY:
        op = random_operator()
        if op not in ops:
            ops.append(op)
    return ops


def random_day_hours():
    engine = round(random.choice([0, 0, 1, 2, 3, 5, 7.5, 8]) + random.uniform(0, 1.5), 1)
    idle = round(random.uniform(0, min(14.0, 20.0 - engine)), 1)
    return engine, idle


def morning_reset(s):
    s["fuel_level"] = round(random.uniform(90, 100), 1)
    s["battery_pct"] = round(random.uniform(95, 100), 1)
    s["engine_temp"] = round(random.uniform(60, 68), 1)
    s["battery_temp"] = round(random.uniform(20, 28), 1)


def start_rental(s, on_date):
    s["rental_days"] = random.randint(10, 30)
    s["check_in"] = on_date
    s["check_out"] = on_date + dt.timedelta(days=s["rental_days"])


def build_fleet(count):
    """Return an ordered list of unit-spec dicts: id, type, site, rental_days."""
    specs = []
    for category, models in CATALOG.items():
        for model in models:
            code = model_code(model)
            # one unit per real site first, so every site holds a few of each model
            order = REAL_SITES[:]
            random.shuffle(order)
            for i in range(count):
                if i < len(order):
                    site = order[i]
                elif random.random() < UNALLOCATED_EXTRA_PROB:
                    site = "NULL"                       # a few left unallocated
                else:
                    site = random.choice(REAL_SITES)
                specs.append({
                    "id": f"{code}-U{i + 1:02d}",
                    "type": category,
                    "site": site,
                    "rental_days": random.randint(10, 30),
                })
    return specs


def init_state(fleet, today):
    state = {}
    for spec in fleet:
        site = spec["site"]
        base_lat, base_lng = SITE_BASE.get(site, SITE_BASE["NULL"])
        rd = spec["rental_days"]
        offset = random.randint(0, max(0, rd - 1))
        check_in = today - dt.timedelta(days=offset)
        engine, idle = random_day_hours()
        roster = three_operators()
        state[spec["id"]] = {
            "type": spec["type"], "site": site,
            "base_lat": base_lat, "base_lng": base_lng,
            "lat": base_lat + random.uniform(-0.002, 0.002),
            "lng": base_lng + random.uniform(-0.002, 0.002),
            "roster": roster, "shift": 0, "operator": roster[0],
            "rental_days": rd,
            "check_in": check_in,
            "check_out": check_in + dt.timedelta(days=rd),
            "engine_hrs": engine, "idle_hrs": idle,
            "engine_temp": random.uniform(70, 90) if engine > 0 else random.uniform(60, 70),
            "battery_pct": random.uniform(70, 100),
            "battery_temp": random.uniform(25, 38),
            "fuel_level": random.uniform(40, 95),
        }
    return state


def new_day(fleet, state, today, shift):
    for spec in fleet:
        s = state[spec["id"]]
        if today >= s["check_out"]:
            start_rental(s, today)
        s["roster"] = three_operators()
        s["shift"] = shift
        s["operator"] = s["roster"][shift]
        s["engine_hrs"], s["idle_hrs"] = random_day_hours()
        morning_reset(s)


def apply_shift(fleet, state, shift):
    for spec in fleet:
        s = state[spec["id"]]
        s["shift"] = shift
        s["operator"] = s["roster"][shift]


def step(fleet, state):
    for spec in fleet:
        s = state[spec["id"]]
        active = s["engine_hrs"] > 0
        if active:
            s["lat"] = round(s["lat"] + random.uniform(-0.00025, 0.00025), 6)
            s["lng"] = round(s["lng"] + random.uniform(-0.00025, 0.00025), 6)
            s["lat"] += (s["base_lat"] - s["lat"]) * 0.05
            s["lng"] += (s["base_lng"] - s["lng"]) * 0.05
        drift = random.uniform(-1.5, 2.5) if active else random.uniform(-2.0, 0.5)
        s["engine_temp"] = clamp(s["engine_temp"] + drift, "engine_temp")
        discharge = random.uniform(0.1, 0.5) if active else random.uniform(0.02, 0.15)
        if random.random() < 0.03:
            s["battery_pct"] = clamp(s["battery_pct"] + random.uniform(5, 20), "battery_pct")
        else:
            s["battery_pct"] = clamp(s["battery_pct"] - discharge, "battery_pct")
        s["battery_temp"] = clamp(s["battery_temp"] + random.uniform(-0.8, 1.0), "battery_temp")
        if active:
            if random.random() < 0.02:
                s["fuel_level"] = clamp(s["fuel_level"] + random.uniform(30, 60), "fuel_level")
            else:
                s["fuel_level"] = clamp(s["fuel_level"] - random.uniform(0.1, 0.6), "fuel_level")


def format_row(eq_id, s, ts):
    return ",".join(str(v) for v in [
        eq_id, s["type"], s["site"],
        s["check_in"].isoformat(), s["check_out"].isoformat(),
        s["engine_hrs"], s["idle_hrs"], s["rental_days"], s["operator"],
        round(s["lat"], 6), round(s["lng"], 6),
        round(s["engine_temp"], 1), round(s["battery_pct"], 1),
        round(s["battery_temp"], 1), round(s["fuel_level"], 1), ts,
    ])


def main():
    p = argparse.ArgumentParser(description="Live synthetic rental telemetry (CSV to stdout)")
    p.add_argument("--count", type=int, default=10, help="units per model (default 10)")
    p.add_argument("--interval", type=float, default=1.0,
                   help="seconds between readings (default 1 = per-second)")
    p.add_argument("--limit", type=int, default=0, help="stop after N rows (0 = infinite)")
    p.add_argument("--no-header", action="store_true", help="suppress CSV header line")
    p.add_argument("--seed", type=int, default=None, help="seed the fleet layout for reproducibility")
    p.add_argument("--day-length", type=float, default=0.0,
                   help="compress one simulated day into this many real seconds "
                        "(shift = day-length/3); default: use real clock")
    args = p.parse_args()

    if args.seed is not None:
        random.seed(args.seed)

    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    if not args.no_header:
        print(",".join(HEADER), flush=True)

    sim_date = dt.date.today()
    fleet = build_fleet(args.count)
    state = init_state(fleet, sim_date)

    if args.day_length > 0:
        cur_shift = 0
    else:
        cur_shift = min(SHIFTS_PER_DAY - 1, dt.datetime.now().hour // 8)
    apply_shift(fleet, state, cur_shift)
    last_shift = cur_shift
    day_started = time.monotonic()
    rows = 0

    def log(msg):
        print(msg, file=sys.stderr, flush=True)

    log(f"# fleet ready: {len(fleet)} units "
        f"({len(fleet) - sum(1 for f in fleet if f['site'] == 'NULL')} allocated, "
        f"{sum(1 for f in fleet if f['site'] == 'NULL')} unallocated)")

    try:
        while True:
            if args.day_length > 0:
                elapsed = time.monotonic() - day_started
                if elapsed >= args.day_length:
                    sim_date += dt.timedelta(days=1)
                    day_started = time.monotonic()
                    cur_shift = 0
                    new_day(fleet, state, sim_date, cur_shift)
                    last_shift = cur_shift
                    log(f"# === NEW DAY: {sim_date.isoformat()} (shift 1) ===")
                else:
                    cur_shift = min(SHIFTS_PER_DAY - 1,
                                    int(elapsed / (args.day_length / SHIFTS_PER_DAY)))
                    if cur_shift != last_shift:
                        apply_shift(fleet, state, cur_shift)
                        last_shift = cur_shift
                        log(f"#   shift {cur_shift + 1} ({sim_date.isoformat()})")
            else:
                real_today = dt.date.today()
                cur_shift = min(SHIFTS_PER_DAY - 1, dt.datetime.now().hour // 8)
                if real_today != sim_date:
                    sim_date = real_today
                    new_day(fleet, state, sim_date, cur_shift)
                    last_shift = cur_shift
                    log(f"# === NEW DAY: {sim_date.isoformat()} (shift {cur_shift + 1}) ===")
                elif cur_shift != last_shift:
                    apply_shift(fleet, state, cur_shift)
                    last_shift = cur_shift
                    log(f"#   shift {cur_shift + 1} ({sim_date.isoformat()})")

            step(fleet, state)
            ts = dt.datetime.now().isoformat(sep=" ", timespec="seconds")
            for spec in fleet:
                print(format_row(spec["id"], state[spec["id"]], ts), flush=True)
                rows += 1
                if args.limit and rows >= args.limit:
                    return
            time.sleep(args.interval)
    except (BrokenPipeError, KeyboardInterrupt):
        pass


if __name__ == "__main__":
    main()