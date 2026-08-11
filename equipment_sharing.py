

import argparse
import csv
import json
import math
import os
import random
import sys
import datetime as dt
TOTAL_HOURS_PER_DAY = 24.0     
DOWNTIME_THRESHOLD_HRS = 6.0   
AVG_CITY_SPEED_KMPH = 25.0     
MAX_NEARBY_KM = 70.0           

DB_DIR = "rental_db"
MASTER_FILE = os.path.join(DB_DIR, "master_fleet.csv")
LISTINGS_FILE = os.path.join(DB_DIR, "listings.json")
SITE_FILE_FMT = os.path.join(DB_DIR, "site_{site}.txt")

SITES = {
    "S001": {"name": "Ambattur, Chennai",           "city": "Chennai",   "lat": 13.1143, "lng": 80.1548},
    "S002": {"name": "Tambaram, Chennai",           "city": "Chennai",   "lat": 12.9249, "lng": 80.1000},
    "S006": {"name": "Sholinganallur (OMR), Chennai", "city": "Chennai", "lat": 12.9010, "lng": 80.2279},
    "S003": {"name": "Whitefield, Bengaluru",       "city": "Bengaluru", "lat": 12.9698, "lng": 77.7500},
    "S004": {"name": "Electronic City, Bengaluru",  "city": "Bengaluru", "lat": 12.8452, "lng": 77.6602},
    "S005": {"name": "Yelahanka, Bengaluru",        "city": "Bengaluru", "lat": 13.1007, "lng": 77.5963},
}

CATALOG = {
    "Skid Steer Loader": ["216B3", "239D3"],
    "Compactor":         ["815", "CB10"],
    "Dozer":             ["D3", "D5", "D11"],
    "Drill":             ["MD6200", "MD6250"],
    "Excavator":         ["320", "330-UHD", "336"],
    "Grader":            ["18", "120"],
    "Off-Highway Truck": ["770", "785"],
}


def haversine_km(lat1, lng1, lat2, lng2):
    """Great-circle distance in km between two lat/lng points."""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return r * 2 * math.asin(math.sqrt(a))


def travel_hours(from_site, to_site):
    """Estimated one-way road travel time (hrs) between two sites."""
    a, b = SITES[from_site], SITES[to_site]
    km = haversine_km(a["lat"], a["lng"], b["lat"], b["lng"])
    return round(km / AVG_CITY_SPEED_KMPH, 2)


def nearby_sites(site):
    """Sites within MAX_NEARBY_KM of the given site (self excluded)."""
    a = SITES[site]
    out = []
    for other, meta in SITES.items():
        if other == site:
            continue
        if haversine_km(a["lat"], a["lng"], meta["lat"], meta["lng"]) <= MAX_NEARBY_KM:
            out.append(other)
    return out


def model_code(model):
    """Filesystem-safe token for a model, so it can live inside an id."""
    return model.replace(" ", "").replace("(", "").replace(")", "")


def build_fake_master(seed=7):
    """
    Create a small, varied fake fleet spread across the real sites.

    Each site gets ~6 machines with random engine/idle hours, deliberately
    seeded so that some machines fall above the downtime threshold (giving the
    sharing flow something to work with).
    """
    random.seed(seed)
    engine_choices = [0, 0, 1, 2, 3, 5, 8]
    idle_choices = [0, 2, 4, 6, 8, 10, 12]

    counters = {}          
    rows = []
    for site in SITES:
        for _ in range(6):
            category = random.choice(list(CATALOG))
            model = random.choice(CATALOG[category])
            code = model_code(model)
            counters[code] = counters.get(code, 0) + 1
            pid = f"{code}-U{counters[code]:02d}"

            engine = random.choice(engine_choices)
            idle = random.choice(idle_choices)
            if engine + idle > TOTAL_HOURS_PER_DAY:     
                idle = max(0, TOTAL_HOURS_PER_DAY - engine)

            rows.append({
                "product_id": pid,
                "type": category,
                "home_site": site,
                "engine_hrs": float(engine),
                "idle_hrs": float(idle),
            })
    return rows


def ensure_db(reset=False):
    """Create the fake database and per-site files if they do not exist."""
    if reset and os.path.isdir(DB_DIR):
        for f in os.listdir(DB_DIR):
            os.remove(os.path.join(DB_DIR, f))

    os.makedirs(DB_DIR, exist_ok=True)
    if os.path.exists(MASTER_FILE) and not reset:
        return

    rows = build_fake_master()

    # 1) master "database" used for processing
    with open(MASTER_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["product_id", "type", "home_site",
                                          "engine_hrs", "idle_hrs"])
        w.writeheader()
        w.writerows(rows)

    # 2) one inventory .txt per site  ->  lines of "PRODUCT_ID,QTY"
    per_site = {s: [] for s in SITES}
    for r in rows:
        per_site[r["home_site"]].append(r["product_id"])
    for site, pids in per_site.items():
        _write_site_entries(site, [(pid, 1) for pid in pids])

    # 3) empty listings state
    _save_listings({"listings": []})
    print(f"[db] fresh fake database created under ./{DB_DIR}/ "
          f"({len(rows)} machines across {len(SITES)} sites)")


def load_master():
    """Return {product_id: record} from the master csv."""
    master = {}
    with open(MASTER_FILE, newline="") as f:
        for row in csv.DictReader(f):
            row["engine_hrs"] = float(row["engine_hrs"])
            row["idle_hrs"] = float(row["idle_hrs"])
            master[row["product_id"]] = row
    return master


def _read_site_entries(site):
    """Return [(product_id, qty), ...] from a site's inventory file."""
    path = SITE_FILE_FMT.format(site=site)
    entries = []
    if not os.path.exists(path):
        return entries
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() for p in line.split(",")]
            pid = parts[0]
            qty = int(parts[1]) if len(parts) > 1 and parts[1] else 1
            entries.append((pid, qty))
    return entries


def _write_site_entries(site, entries):
    """Overwrite a site's inventory file. Format stays 'PRODUCT_ID,QTY'."""
    path = SITE_FILE_FMT.format(site=site)
    meta = SITES.get(site, {"name": site})
    with open(path, "w") as f:
        f.write(f"# Site {site} ({meta['name']}) equipment inventory\n")
        f.write("# Edit this file by adding/removing a line: PRODUCT_ID,QTY\n")
        for pid, qty in entries:
            f.write(f"{pid},{qty}\n")


def add_product(site, pid, qty=1):
    """Add (or top-up) a product id + quantity in a site file."""
    entries = _read_site_entries(site)
    for i, (existing, q) in enumerate(entries):
        if existing == pid:
            entries[i] = (pid, q + qty)
            _write_site_entries(site, entries)
            print(f"[db] {pid} quantity in {site} increased to {q + qty}")
            return
    entries.append((pid, qty))
    _write_site_entries(site, entries)
    print(f"[db] added {pid} (qty {qty}) to {site}")


def remove_product(site, pid):
    """Remove a product id from a site file."""
    entries = _read_site_entries(site)
    kept = [(p, q) for p, q in entries if p != pid]
    if len(kept) == len(entries):
        print(f"[db] {pid} was not in {site}")
        return
    _write_site_entries(site, kept)
    print(f"[db] removed {pid} from {site}")


def _load_listings():
    with open(LISTINGS_FILE) as f:
        return json.load(f)


def _save_listings(data):
    with open(LISTINGS_FILE, "w") as f:
        json.dump(data, f, indent=2)


def downtime_hours(rec):
    """downtime = TOTAL_HOURS_PER_DAY - engine - idle (never negative)."""
    return round(max(0.0, TOTAL_HOURS_PER_DAY - rec["engine_hrs"] - rec["idle_hrs"]), 1)


def underutilised_units(site, master):
    """Native machines of this site whose downtime exceeds the threshold."""
    out = []
    for rec in master.values():
        if rec["home_site"] != site:
            continue
        if downtime_hours(rec) > DOWNTIME_THRESHOLD_HRS:
            out.append(rec)
    return sorted(out, key=lambda r: downtime_hours(r), reverse=True)


def _already_listed(listings, pid, to_site):
    """True if this machine is already offered to that site (not declined)."""
    for l in listings["listings"]:
        if l["product_id"] == pid and l["to_site"] == to_site and l["status"] != "declined":
            return True
    return False


def ask_yes_no(prompt):
    while True:
        ans = input(f"{prompt} (y/n): ").strip().lower()
        if ans in ("y", "yes"):
            return True
        if ans in ("n", "no"):
            return False
        print("  please answer y or n.")


def choose_site():
    print("\nSites:")
    for code, meta in SITES.items():
        print(f"  {code}  -  {meta['name']} ({meta['city']})")
    while True:
        site = input("\nWhich site do you belong to? (e.g. S001): ").strip().upper()
        if site in SITES:
            return site
        print("  unknown site code, try again.")


def _rule(char="-", n=64):
    print(char * n)


def step_offer(site, master, listings):
    _rule("=")
    print(f"STEP 2  Idle machines at {site} ({SITES[site]['name']})")
    _rule("=")

    idle = underutilised_units(site, master)
    if not idle:
        print("No machine here is idle beyond "
              f"{DOWNTIME_THRESHOLD_HRS:.0f} hrs. Nothing to offer.\n")
        return

    neighbours = nearby_sites(site)
    made_offer = False
    shown = False

    for rec in idle:
        pid = rec["product_id"]
        dt_hrs = downtime_hours(rec)

        targets = []
        for nb in neighbours:
            travel = travel_hours(site, nb)
            available = round(dt_hrs - travel, 1)
            if available <= 0:
                continue
            if _already_listed(listings, pid, nb):
                continue
            targets.append((nb, travel, available))

        if not targets:
            continue

        shown = True
        print(f"\n  {pid}  [{rec['type']}]")
        print(f"    engine {rec['engine_hrs']:.1f}h + idle {rec['idle_hrs']:.1f}h "
              f"-> downtime {dt_hrs:.1f}h")
        print("    can be listed to nearby sites:")
        for nb, travel, available in targets:
            print(f"      {nb} ({SITES[nb]['name']}): "
                  f"travel {travel:.2f}h -> available {available:.1f}h")

        if ask_yes_no(f"    List {pid} to these nearby sites?"):
            for nb, travel, available in targets:
                listings["listings"].append({
                    "id": f"L{len(listings['listings']) + 1:04d}",
                    "product_id": pid,
                    "type": rec["type"],
                    "from_site": site,
                    "to_site": nb,
                    "downtime_hrs": dt_hrs,
                    "travel_hrs": travel,
                    "available_hrs": available,
                    "status": "pending",
                    "created": dt.datetime.now().isoformat(timespec="seconds"),
                })
            made_offer = True
            print(f"    listed to {', '.join(t[0] for t in targets)}.")
        else:
            print("    left in place.")

    if not shown:
        print("All idle machines here are already listed to nearby sites.")
    if made_offer:
        _save_listings(listings)
    print()


def step_review(site, master, listings):
    _rule("=")
    print(f"STEP 3  Machines offered to {site} ({SITES[site]['name']})")
    _rule("=")

    pending = [l for l in listings["listings"]
               if l["to_site"] == site and l["status"] == "pending"]
    if not pending:
        print("No machines are currently offered to this site.\n")
        return

    changed = False
    for l in pending:
        print(f"\n  {l['product_id']}  [{l['type']}]  from {l['from_site']} "
              f"({SITES[l['from_site']]['name']})")
        print(f"    available here: {l['available_hrs']:.1f}h "
              f"(primary downtime {l['downtime_hrs']:.1f}h - travel {l['travel_hrs']:.2f}h)")

        if ask_yes_no(f"    Approve {l['product_id']} for shared use at {site}?"):
            l["status"] = "accepted"
            add_product(site, l["product_id"], 1)   # now lives in two site files
            print(f"    approved. {l['product_id']} now serves "
                  f"{l['from_site']} + {site} (multi-site).")
        else:
            l["status"] = "declined"
            print("    declined.")
        changed = True

    if changed:
        _save_listings(listings)
    print()


def main():
    p = argparse.ArgumentParser(
        description="Cross-site idle-equipment sharing for the Smart Rental Tracking System.")
    p.add_argument("--site", help="site code to operate as (skips the prompt)")
    p.add_argument("--reset", action="store_true", help="rebuild the fake database")
    p.add_argument("--add", nargs=3, metavar=("SITE", "PRODUCT_ID", "QTY"),
                   help="add a product id + quantity to a site file")
    p.add_argument("--remove", nargs=2, metavar=("SITE", "PRODUCT_ID"),
                   help="remove a product id from a site file")
    args = p.parse_args()

    ensure_db(reset=args.reset)
    if args.reset:
        return

    if args.add:
        site, pid, qty = args.add
        add_product(site.upper(), pid, int(qty))
        return
    if args.remove:
        site, pid = args.remove
        remove_product(site.upper(), pid)
        return


    site = (args.site.upper() if args.site else choose_site())
    if site not in SITES:
        print(f"unknown site: {site}")
        sys.exit(1)

    master = load_master()
    listings = _load_listings()

    step_offer(site, master, listings)     
    step_review(site, master, listings)    

    print("Done. (Approved machines were added to the receiving site file; "
          "outgoing offers wait until that site runs this tool.)")


if __name__ == "__main__":
    main()