import argparse
import csv
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser

import requests
from bs4 import BeautifulSoup


FLEET = {
    "Skid Steer Loader": "216B3",
    "Compactor":         "CB10",
    "Dozer":             "D5",
    "Dragline":          "8200",
    "Drill":             "MD6250",
    "Excavator":         "320",
    "Grader":            "120",
    "Off-Highway Truck": "770",
}

PLAN_SIGNALS = {
    "Excavator":         [("excavat", 3), ("earthwork", 2), ("foundation", 2),
                          ("trench", 2), ("digging", 2), ("site prepar", 1),
                          ("infrastructure", 1)],
    "Dozer":             [("bulldoz", 3), ("doz", 2), ("land clear", 2),
                          ("grading", 1), ("push", 1), ("levelling", 1)],
    "Grader":            [("grader", 3), ("road", 2), ("highway", 2),
                          ("pavement", 1), ("levelling", 1)],
    "Compactor":         [("compact", 3), ("roller", 2), ("asphalt", 2),
                          ("pavement", 1), ("road", 1)],
    "Skid Steer Loader": [("skid steer", 3), ("loader", 2), ("landscap", 1),
                          ("material handling", 1)],
    "Drill":             [("drill", 3), ("blast", 2), ("bore", 2),
                          ("exploration", 2), ("mineral", 1)],
    "Dragline":          [("dragline", 3), ("overburden", 3), ("open pit", 2),
                          ("open-cast", 2), ("stripping", 2)],
    "Off-Highway Truck": [("haul", 3), ("dump truck", 3), ("off-highway", 3),
                          ("ore transport", 2), ("tonnage", 1), ("mine", 1)],
}

MEMBERSHIP = {
    "Excavator": ("construction", "mining"),
    "Dozer": ("construction",), "Grader": ("construction",),
    "Compactor": ("construction",), "Skid Steer Loader": ("construction",),
    "Drill": ("mining",), "Dragline": ("mining",), "Off-Highway Truck": ("mining",),
}

CONSTRUCTION_CUES = ["construction", "building", "highway", "road", "metro",
                     "infrastructure", "real estate", "township", "bridge",
                     "airport", "port", "smart city", "housing"]
MINING_CUES = ["mining", "mine", "coal", "iron ore", "ore", "mineral",
               "quarry", "extraction", "open pit", "open-cast", "overburden",
               "colliery", "bauxite", "lignite"]
SCALE_CUES = ["crore", "billion", "million", "mw", "gw", " km", "expansion",
              "new project", "tender", "awarded", "phase", "capacity",
              "greenfield", "brownfield"]

WEATHER_ADVERSE = ["monsoon", "heavy rain", "flood", "cyclone", "storm",
                   "waterlogg", "landslide", "snow", "downpour", "red alert"]
WEATHER_FAVORABLE = ["clear", "dry", "sunny", "fair weather", "post-monsoon",
                     "favourable", "favorable", "normal weather"]

ADVERSE_MULTIPLIER = 0.7
FAVORABLE_MULTIPLIER = 1.15
BASELINE_UNITS = 2
MAX_UNITS = 12


def categories():
    return list(FLEET)


OLLAMA_URL = "http://localhost:11434"
DEFAULT_MODEL = "llama3.2:1b"


def ollama_available(timeout=3):
    try:
        req = urllib.request.Request(OLLAMA_URL + "/api/tags")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError):
        return False


def ollama_models(timeout=3):
    try:
        req = urllib.request.Request(OLLAMA_URL + "/api/tags")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            tags = json.loads(resp.read().decode("utf-8"))
        return [m.get("name", "") for m in tags.get("models", [])]
    except (urllib.error.URLError, OSError):
        return []


def ollama_generate(prompt, model=DEFAULT_MODEL, timeout=180, temperature=0.2):
    """Single-shot prompt to a local Ollama server; None if unavailable."""
    if not ollama_available():
        return None
    try:
        data = json.dumps({"model": model, "prompt": prompt, "stream": False,
                           "options": {"temperature": temperature}}).encode("utf-8")
        req = urllib.request.Request(OLLAMA_URL + "/api/generate", data=data,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8")).get("response")
    except (urllib.error.URLError, OSError, ValueError) as e:
        print(f"  ! Ollama generate failed: {e}")
        return None


USER_AGENT = ("Mozilla/5.0 (compatible; SmartRentalDemandBot/1.0; "
              "+internal fleet planning)")
TIMEOUT = 20
STRIP_TAGS = ["script", "style", "noscript", "svg", "form", "header",
              "footer", "nav", "aside"]


def read_url_list(path):
    urls = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                urls.append(line)
    return urls


def read_sources_csv(path):
   
    sources = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            url = (row.get("url") or "").strip()
            if not url:
                continue
            sources.append({
                "category": (row.get("category") or "").strip(),
                "name": (row.get("name") or url).strip(),
                "url": url,
                "type": (row.get("type") or "").strip(),
                "usability": (row.get("scrape_usability") or "").strip(),
                "notes": (row.get("notes") or "").strip(),
            })
    return sources


def group_sources(sources):
    groups = {"weather": [], "plans": []}
    for s in sources:
        cat = s["category"].lower()
        if cat == "weather":
            groups["weather"].append(s)
        elif cat in ("construction", "mining"):
            groups["plans"].append(s)
    return groups


def robots_allows(url):
    try:
        parts = urllib.parse.urlparse(url)
        if parts.scheme not in ("http", "https"):
            return True
        rp = urllib.robotparser.RobotFileParser()
        rp.set_url(f"{parts.scheme}://{parts.netloc}/robots.txt")
        rp.read()
        return rp.can_fetch(USER_AGENT, url)
    except Exception:
        return True


def fetch(url):
    parts = urllib.parse.urlparse(url)
    if parts.scheme in ("", "file") or os.path.exists(url):
        path = parts.path if parts.scheme == "file" else url
        try:
            with open(path, encoding="utf-8", errors="ignore") as f:
                return f.read()
        except OSError as e:
            print(f"  ! cannot read local file {url}: {e}", file=sys.stderr)
            return None
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
        resp.raise_for_status()
        return resp.text
    except requests.RequestException as e:
        print(f"  ! fetch failed for {url}: {e}", file=sys.stderr)
        return None


def extract_text(html):
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(STRIP_TAGS):
        tag.decompose()

    parts = []
    if soup.title and soup.title.string:
        parts.append(f"TITLE: {soup.title.string.strip()}")
    desc = soup.find("meta", attrs={"name": "description"})
    if desc and desc.get("content"):
        parts.append(f"DESCRIPTION: {desc['content'].strip()}")
    for tag in soup.find_all(["h1", "h2", "h3", "h4", "p", "li", "td", "th",
                              "caption", "figcaption"]):
        text = " ".join(tag.get_text(" ", strip=True).split())
        if len(text) >= 3:
            parts.append(text)

    seen, cleaned = set(), []
    for line in parts:
        key = line.lower()
        if key not in seen:
            seen.add(key)
            cleaned.append(line)
    return "\n".join(cleaned)


def scrape(label, urls, out_path, delay=1.0, ignore_robots=False):
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    blocks, ok = [], 0
    for i, url in enumerate(urls, 1):
        print(f"[{label}] ({i}/{len(urls)}) {url}")
        if not ignore_robots and not robots_allows(url):
            print("  - skipped (robots.txt disallow)", file=sys.stderr)
            continue
        html = fetch(url)
        if not html:
            continue
        text = extract_text(html)
        if not text.strip():
            print("  - no readable text extracted", file=sys.stderr)
            continue
        blocks.append(f"===== SOURCE [{label}] {url} =====\n{text}\n")
        ok += 1
        if delay and url.startswith("http"):
            time.sleep(delay)

    header = (f"# SCRAPE LABEL: {label}\n# SOURCES OK: {ok}/{len(urls)}\n"
              f"# CHARS: {sum(len(b) for b in blocks)}\n\n")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(header + "\n".join(blocks))
    print(f"[{label}] wrote {ok}/{len(urls)} sources -> {out_path}")
    return ok


def scrape_sources(label, sources, out_path, delay=1.0, ignore_robots=False):
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    blocks, fetched = [], 0
    for i, s in enumerate(sources, 1):
        url = s["url"]
        print(f"[{label}] ({i}/{len(sources)}) {s['name']} - {url}")
        page = ""
        if ignore_robots or robots_allows(url):
            html = fetch(url)
            if html:
                page = extract_text(html)
                fetched += 1
            else:
                print("  - page not fetched; using CSV notes only", file=sys.stderr)
        else:
            print("  - robots.txt disallow; using CSV notes only", file=sys.stderr)
        manifest = (f"NAME: {s['name']} | CATEGORY: {s['category']} | "
                    f"TYPE: {s['type']} | USABILITY: {s['usability']}\n"
                    f"NOTES: {s['notes']}")
        blocks.append(f"===== SOURCE [{label}] {s['name']} {url} =====\n"
                      f"{manifest}\n{page}\n")
        if delay and url.startswith("http"):
            time.sleep(delay)

    header = (f"# SCRAPE LABEL: {label}\n"
              f"# SOURCES: {len(sources)} (pages fetched: {fetched}, "
              f"notes-only: {len(sources) - fetched})\n"
              f"# CHARS: {sum(len(b) for b in blocks)}\n\n")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(header + "\n".join(blocks))
    print(f"[{label}] {len(sources)} sources ({fetched} pages fetched) -> {out_path}")
    return len(sources)
CHAR_BUDGET = 6000


def read_text(path):
    if not path or not os.path.exists(path):
        return ""
    with open(path, encoding="utf-8", errors="ignore") as f:
        return f.read()


def count_hits(text, terms):
    t = text.lower()
    return sum(t.count(term.lower()) for term in terms)


def heuristic_analyze(weather_text, plans_text):
    w, p = weather_text.lower(), plans_text.lower()

    adverse = count_hits(w, WEATHER_ADVERSE)
    favorable = count_hits(w, WEATHER_FAVORABLE)
    if adverse > favorable:
        weather_mult, weather_state = ADVERSE_MULTIPLIER, "ADVERSE"
    elif favorable > adverse:
        weather_mult, weather_state = FAVORABLE_MULTIPLIER, "FAVORABLE"
    else:
        weather_mult, weather_state = 1.0, "NEUTRAL"

    constr = count_hits(p, CONSTRUCTION_CUES)
    mining = count_hits(p, MINING_CUES)
    total = constr + mining or 1
    emphasis = {"construction": constr / total, "mining": mining / total}

    scale_hits = count_hits(p, SCALE_CUES)
    scale_nudge = min(1.2, 1.0 + 0.01 * scale_hits)

    raw = {}
    for cat in categories():
        base = sum(wt * p.count(term) for term, wt in PLAN_SIGNALS[cat])
        domain_boost = sum(emphasis[d] for d in MEMBERSHIP[cat]) * 3.0
        raw[cat] = base + domain_boost
    max_raw = max(raw.values()) or 1.0

    demand = []
    span = MAX_UNITS - BASELINE_UNITS
    for cat in categories():
        norm = raw[cat] / max_raw
        units = (BASELINE_UNITS + norm * span) * weather_mult * scale_nudge
        units = max(0, min(MAX_UNITS, int(round(units))))
        priority = "high" if units >= 6 else "medium" if units >= 3 else "low"
        top = [term for term, _ in PLAN_SIGNALS[cat] if term in p][:3]
        reason = (f"plan signals {top or 'baseline'}; "
                  f"{'/'.join(MEMBERSHIP[cat])} emphasis; weather {weather_state}")
        demand.append({"category": cat, "model": FLEET[cat],
                       "recommended_units": units, "priority": priority,
                       "reason": reason})

    demand.sort(key=lambda d: d["recommended_units"], reverse=True)
    return {
        "engine": "heuristic",
        "weather_outlook": f"{weather_state} (adverse cues={adverse}, favorable={favorable}) "
                           f"-> demand multiplier {weather_mult}",
        "future_plans": f"construction {emphasis['construction']*100:.0f}% / "
                        f"mining {emphasis['mining']*100:.0f}%, scale signals={scale_hits}",
        "comparison": f"Plans set per-category need; weather ({weather_state}) "
                      f"scales it by {weather_mult}x for the near term.",
        "demand": demand,
    }


def build_prompt(weather_text, plans_text, memory_tail_text):
    cats = ", ".join(categories())
    return f"""You are a fleet-planning analyst for an equipment rental company.
Decide how many of each vehicle category to STOCK UP based on the two sources below.

VEHICLE CATEGORIES (use ONLY these): {cats}

WEATHER SOURCE (affects near-term demand: bad weather lowers earthworks, good weather raises it):
\"\"\"{weather_text[:CHAR_BUDGET]}\"\"\"

CONSTRUCTION / MINING FUTURE-PLANS SOURCE (drives which machines are needed and how many):
\"\"\"{plans_text[:CHAR_BUDGET]}\"\"\"

PREVIOUS ASSESSMENTS (for continuity, may be empty):
\"\"\"{memory_tail_text}\"\"\"

TASK:
1. Summarise the weather outlook.
2. Summarise the construction/mining future plans.
3. Compare them: how weather adjusts the plan-driven demand.
4. Output a demand list: for EACH relevant category, recommended_units (integer 0-12),
   a priority (high/medium/low) and a one-line reason.

Respond with ONLY valid JSON, no prose, in exactly this shape:
{{"weather_outlook":"...","future_plans":"...","comparison":"...",
"demand":[{{"category":"Excavator","recommended_units":6,"priority":"high","reason":"..."}}]}}"""


def parse_llm_json(raw):
    if not raw:
        return None
    cleaned = re.sub(r"```(json)?", "", raw).strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        data = json.loads(cleaned[start:end + 1])
    except json.JSONDecodeError:
        return None
    if "demand" not in data or not isinstance(data["demand"], list):
        return None
    valid = []
    for d in data["demand"]:
        cat = d.get("category")
        if cat in FLEET:
            try:
                units = int(round(float(d.get("recommended_units", 0))))
            except (TypeError, ValueError):
                units = 0
            valid.append({"category": cat, "model": FLEET[cat],
                          "recommended_units": max(0, min(MAX_UNITS, units)),
                          "priority": d.get("priority", "medium"),
                          "reason": d.get("reason", "")})
    if not valid:
        return None
    valid.sort(key=lambda x: x["recommended_units"], reverse=True)
    data["demand"] = valid
    data["engine"] = "ollama"
    return data


def render_list(result):
    bar, dash = "=" * 60, "-" * 60
    lines = [bar, "     VEHICLES TO STOCK UP  (Smart Rental demand forecast)", bar,
             f" Engine     : {result['engine']}",
             f" Weather    : {result['weather_outlook']}",
             f" Plans      : {result['future_plans']}",
             f" Comparison : {result['comparison']}", dash,
             f" {'CATEGORY':<20}{'MODEL':<9}{'UNITS':>6}  PRIORITY", dash]
    for d in result["demand"]:
        lines.append(f" {d['category']:<20}{d['model']:<9}"
                     f"{d['recommended_units']:>6}  {d['priority']}")
    lines.append(dash)
    lines.append(" REASONS")
    for d in result["demand"]:
        lines.append(f"  - {d['category']} x{d['recommended_units']}: {d['reason']}")
    lines.append(bar)
    return "\n".join(lines)


def append_memory(memory_path, result):
    ts = dt.datetime.now().isoformat(timespec="seconds")
    top = ", ".join(f"{d['category']} x{d['recommended_units']}"
                    for d in result["demand"][:4])
    with open(memory_path, "a", encoding="utf-8") as f:
        f.write(f"[{ts}] engine={result['engine']} | top: {top}\n")


def memory_tail(memory_path, n=5):
    if not os.path.exists(memory_path):
        return ""
    with open(memory_path, encoding="utf-8") as f:
        return "".join(f.readlines()[-n:])


def run_bot(weather_path, plans_path, model, no_llm, outdir):
    weather_text = read_text(weather_path)
    plans_text = read_text(plans_path)
    if not plans_text.strip():
        print(f"! no plans text in {plans_path} - run the scraper first.")

    os.makedirs(outdir, exist_ok=True)
    mem_path = os.path.join(outdir, "memory.txt")

    result = None
    if not no_llm and ollama_available():
        print(f"# using Ollama model '{model}' "
              f"(available: {', '.join(ollama_models()) or 'none'})")
        raw = ollama_generate(build_prompt(weather_text, plans_text,
                                           memory_tail(mem_path)), model=model)
        result = parse_llm_json(raw)
        if result is None:
            print("# Ollama output unusable - falling back to heuristic.")
    elif not no_llm:
        print("# Ollama not reachable on localhost:11434 - using heuristic.")

    if result is None:
        result = heuristic_analyze(weather_text, plans_text)

    listing = render_list(result)
    print("\n" + listing)
    with open(os.path.join(outdir, "demand_vehicles.txt"), "w", encoding="utf-8") as f:
        f.write(listing + "\n")
    with open(os.path.join(outdir, "demand_vehicles.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)
    append_memory(mem_path, result)
    print(f"\n# wrote {outdir}/demand_vehicles.txt, demand_vehicles.json; "
          f"memory appended to {mem_path}")
    return result


# =========================================================================== #
#  DEMO - materialise sample url files + html fixtures for an offline run
# =========================================================================== #
def write_demo():
    os.makedirs("fixtures", exist_ok=True)
    files = {
        "urls_weather.txt":
            "# One WEATHER URL per line (# comments ok; local .html works too).\n"
            "fixtures/weather.html\n",
        "urls_plans.txt":
            "# One CONSTRUCTION/MINING future-plan URL per line.\n"
            "fixtures/plans_construction.html\nfixtures/plans_mining.html\n",
        "fixtures/weather.html":
            "<html><head><title>Regional Weather Outlook</title>"
            "<meta name='description' content='Heavy rain and monsoon alerts.'></head>"
            "<body><h1>Monsoon intensifies over Tamil Nadu and Karnataka</h1>"
            "<p>IMD red alert for heavy rain and waterlogging across Chennai over "
            "the next week. A cyclonic circulation brings downpour and possible "
            "flooding to low-lying construction sites; Bengaluru sees storms.</p>"
            "</body></html>",
        "fixtures/plans_construction.html":
            "<html><head><title>Infrastructure Pipeline 2026</title></head><body>"
            "<h1>State awards Rs 12,000 crore highway and metro package</h1>"
            "<p>Greenfield expressway tender covering 240 km of new highway needs "
            "large-scale earthwork, grading and road pavement. A metro phase adds "
            "foundation and excavation works plus compaction and asphalt paving; "
            "site preparation begins with heavy earthmoving and bulldozer land "
            "clearing.</p></body></html>",
        "fixtures/plans_mining.html":
            "<html><head><title>Coal Mining Expansion</title></head><body>"
            "<h1>Coal India approves brownfield open-cast expansion</h1>"
            "<p>Adds overburden stripping capacity at two open pit coal mines with "
            "additional dragline deployment, heavy drilling and blasting, and "
            "off-highway haul truck fleets to move increased ore tonnage. An iron "
            "ore greenfield project needs exploration drilling and bore work with "
            "new haul roads for dump trucks.</p></body></html>",
    }
    for path, content in files.items():
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
    print("demo files written: urls_weather.txt, urls_plans.txt, fixtures/*.html")


# =========================================================================== #
#  CLI
# =========================================================================== #
def main():
    p = argparse.ArgumentParser(
        description="Scrape two URL sets, compare weather vs plans, list vehicles to stock up.")
    sub = p.add_subparsers(dest="cmd")

    sp = sub.add_parser("scrape", help="scrape one URL set to a text file")
    sp.add_argument("--label", required=True)
    sp.add_argument("--urls", required=True)
    sp.add_argument("--out", required=True)
    sp.add_argument("--delay", type=float, default=1.0)
    sp.add_argument("--ignore-robots", action="store_true")

    bp = sub.add_parser("bot", help="analyse scraped text -> stock-up list")
    bp.add_argument("--weather", default="scraped/weather.txt")
    bp.add_argument("--plans", default="scraped/plans.txt")
    bp.add_argument("--model", default=DEFAULT_MODEL)
    bp.add_argument("--no-llm", action="store_true")
    bp.add_argument("--outdir", default="output")

    rp = sub.add_parser("run", help="scrape both sets then analyse (full pipeline)")
    rp.add_argument("--sources", default="data_sources.csv",
                    help="manually-maintained CSV of links (category,name,url,...,notes)")
    rp.add_argument("--weather-urls", default="urls_weather.txt")
    rp.add_argument("--plans-urls", default="urls_plans.txt")
    rp.add_argument("--model", default=DEFAULT_MODEL)
    rp.add_argument("--no-llm", action="store_true")
    rp.add_argument("--delay", type=float, default=1.0)
    rp.add_argument("--ignore-robots", action="store_true")
    rp.add_argument("--outdir", default="output")

    src = sub.add_parser("sources", help="preview how data_sources.csv is grouped")
    src.add_argument("--sources", default="data_sources.csv")

    sub.add_parser("demo", help="write sample url files + html fixtures")

    args = p.parse_args()
    if args.cmd is None:                 # no subcommand -> default to a full run,
        args = p.parse_args(["run"])     # re-parse so run's arguments exist
    cmd = args.cmd

    if cmd == "demo":
        write_demo()

    elif cmd == "scrape":
        urls = read_url_list(args.urls)
        if not urls:
            sys.exit(f"No URLs found in {args.urls}")
        scrape(args.label, urls, args.out, args.delay, args.ignore_robots)

    elif cmd == "sources":
        if not os.path.exists(args.sources):
            sys.exit(f"{args.sources} not found.")
        groups = group_sources(read_sources_csv(args.sources))
        for label in ("weather", "plans"):
            print(f"\n[{label}] {len(groups[label])} sources")
            for s in groups[label]:
                print(f"  - {s['category']:<13} {s['name']}  ({s['url']})")

    elif cmd == "bot":
        run_bot(args.weather, args.plans, args.model, args.no_llm, args.outdir)

    elif cmd == "run":
        if os.path.exists(args.sources):
            groups = group_sources(read_sources_csv(args.sources))
            print(f"# sources from {args.sources}: "
                  f"{len(groups['weather'])} weather, "
                  f"{len(groups['plans'])} construction/mining")
            print("STEP 1/3  scraping weather sources")
            scrape_sources("weather", groups["weather"], "scraped/weather.txt",
                           args.delay, args.ignore_robots)
            print("\nSTEP 2/3  scraping construction/mining future-plan sources")
            scrape_sources("plans", groups["plans"], "scraped/plans.txt",
                           args.delay, args.ignore_robots)
        else:
            if not (os.path.exists(args.weather_urls) and os.path.exists(args.plans_urls)):
                print(f"# {args.sources} and url files missing - writing demo fixtures.")
                write_demo()
            print("STEP 1/3  scraping weather URLs")
            scrape("weather", read_url_list(args.weather_urls),
                   "scraped/weather.txt", args.delay, args.ignore_robots)
            print("\nSTEP 2/3  scraping construction/mining future-plan URLs")
            scrape("plans", read_url_list(args.plans_urls),
                   "scraped/plans.txt", args.delay, args.ignore_robots)

        print("\nSTEP 3/3  analysing and building the stock-up list")
        run_bot("scraped/weather.txt", "scraped/plans.txt",
                args.model, args.no_llm, args.outdir)


if __name__ == "__main__":
    main()