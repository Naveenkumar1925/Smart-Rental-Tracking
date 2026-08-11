
import argparse
import base64
import datetime as dt
import json
import os
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from collections import OrderedDict

from flask import Flask, Response, jsonify

# --------------------------------------------------------------------------- #
#  Column order emitted by data_synth.py (must match HEADER there)
# --------------------------------------------------------------------------- #
COLS = [
    "equipment_id", "equipment_type", "site_id", "check_in", "check_out",
    "engine_hrs", "idle_hrs", "rental_days", "operator",
    "lat", "lng", "engine_temp", "battery_pct", "battery_temp", "fuel_level",
]

# --------------------------------------------------------------------------- #
#  Site definitions.  Base coords come from data_synth.SITE_BASE.
#  Each site gets a FIXED quadrilateral (4 outer corners) built once at import
#  from a fixed offset pattern, so boundaries never change between runs.
# --------------------------------------------------------------------------- #
SITE_BASE = {
    # Chennai, Tamil Nadu — three different localities
    "S001": (13.1143, 80.1548),   # Ambattur, Chennai
    "S002": (12.9249, 80.1000),   # Tambaram, Chennai
    "S006": (12.9010, 80.2279),   # Sholinganallur (OMR), Chennai
    # Bengaluru, Karnataka — three different localities
    "S003": (12.9698, 77.7500),   # Whitefield, Bengaluru
    "S004": (12.8452, 77.6602),   # Electronic City, Bengaluru
    "S005": (13.1007, 77.5963),   # Yelahanka, Bengaluru
}

SITE_KIND = {
    "S001": "construction", "S002": "construction", "S003": "construction",
    "S004": "mining",       "S005": "mining",       "S006": "mining",
}

# Fixed corner offsets (dlat, dlng) applied to each site's base point, in order
# NE, SE, SW, NW.  Deliberately slightly irregular so it reads as a real plot,
# but constant -> the 4 corners are "fixed". Half-size ~0.0032 deg (~350 m).
_D = 0.0032
_CORNER_PATTERN = [
    ( 1.00 * _D,  1.10 * _D),   # NE
    (-1.05 * _D,  0.95 * _D),   # SE
    (-1.00 * _D, -1.10 * _D),   # SW
    ( 1.08 * _D, -0.95 * _D),   # NW
]


def build_sites():
    sites = OrderedDict()
    for sid, (blat, blng) in SITE_BASE.items():
        corners = [(round(blat + dlat, 6), round(blng + dlng, 6))
                   for dlat, dlng in _CORNER_PATTERN]
        sites[sid] = {
            "site_id": sid,
            "kind": SITE_KIND[sid],
            "center": [blat, blng],
            "corners": corners,          # list of [lat, lng], 4 outer corners
        }
    return sites


SITES = build_sites()


def point_in_polygon(lat, lng, corners):
    inside = False
    n = len(corners)
    j = n - 1
    for i in range(n):
        yi, xi = corners[i]          # (lat, lng)
        yj, xj = corners[j]
        if ((yi > lat) != (yj > lat)) and \
           (lng < (xj - xi) * (lat - yi) / (yj - yi + 1e-15) + xi):
            inside = not inside
        j = i
    return inside


class Alerter:
    def __init__(self, cooldown=60):
        self.sid = os.environ.get("TWILIO_ACCOUNT_SID", "")
        self.token = os.environ.get("TWILIO_AUTH_TOKEN", "")
        self.from_ = os.environ.get("TWILIO_FROM", "")
        # default destination = the number you gave; override with TWILIO_TO
        # (comma-separated for up to two numbers)
        to = os.environ.get("TWILIO_TO", "+918248377632")
        self.to = [n.strip() for n in to.split(",") if n.strip()]
        self.enabled = bool(self.sid and self.token and self.from_ and self.to)
        self.cooldown = cooldown
        self._last = {}              # (eq_id, site_id) -> last-sent monotonic
        if not self.enabled:
            print("[twilio] not fully configured -> SMS will be SIMULATED "
                  "(set TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN / TWILIO_FROM / "
                  "TWILIO_TO)", file=sys.stderr, flush=True)

    def _send_one(self, to_number, body):
        url = f"https://api.twilio.com/2010-04-01/Accounts/{self.sid}/Messages.json"
        data = urllib.parse.urlencode(
            {"To": to_number, "From": self.from_, "Body": body}).encode()
        auth = base64.b64encode(f"{self.sid}:{self.token}".encode()).decode()
        req = urllib.request.Request(url, data=data,
                                     headers={"Authorization": f"Basic {auth}"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status

    def breach(self, eq_id, eq_type, site_id, lat, lng):
        key = (eq_id, site_id)
        now = time.monotonic()
        if now - self._last.get(key, -1e9) < self.cooldown:
            return                       # throttle repeats for the same unit
        self._last[key] = now
        body = (f"GEOFENCE BREACH: {eq_id} ({eq_type}) has left site {site_id}. "
                f"Position {lat:.5f},{lng:.5f} @ {dt.datetime.now():%H:%M:%S}.")
        if not self.enabled:
            print(f"[SMS SIMULATED -> {', '.join(self.to)}] {body}",
                  file=sys.stderr, flush=True)
            return
        for number in self.to[:2]:       # the two specified numbers
            try:
                self._send_one(number, body)
                print(f"[SMS SENT -> {number}] {body}", file=sys.stderr, flush=True)
            except Exception as exc:      # noqa: BLE001 - never crash the stream
                print(f"[SMS FAILED -> {number}] {exc}", file=sys.stderr, flush=True)


class Fleet:
    def __init__(self, alerter):
        self.lock = threading.Lock()
        self.vehicles = {}               # eq_id -> latest record dict
        self.breaches = []               # rolling log of recent breach events
        self.held = {}                   # eq_id -> forced (lat,lng) for demo holds
        self.alerter = alerter

    def hold_out(self, eq_id, lat, lng):
        with self.lock:
            self.held[eq_id] = (lat, lng)

    def release(self, eq_id):
        with self.lock:
            self.held.pop(eq_id, None)

    def update(self, row):
        rec = dict(zip(COLS, row))
        try:
            lat = float(rec["lat"]); lng = float(rec["lng"])
        except ValueError:
            return
        eq_id = rec["equipment_id"]

        with self.lock:
            held = self.held.get(eq_id)
        if held:                         # keep a demo-breached unit outside
            lat, lng = held

        site_id = rec["site_id"]
        out_of_bounds = False
        if site_id in SITES:             # NULL / yard units are never geofenced
            inside = point_in_polygon(lat, lng, SITES[site_id]["corners"])
            out_of_bounds = not inside

        with self.lock:
            prev = self.vehicles.get(eq_id)
            was_out = prev.get("out_of_bounds", False) if prev else False
            rec.update({"lat": lat, "lng": lng, "out_of_bounds": out_of_bounds,
                        "ts": dt.datetime.now().isoformat(timespec="seconds")})
            self.vehicles[eq_id] = rec
            if out_of_bounds and not was_out:      # log only the transition
                self.breaches.insert(0, {
                    "equipment_id": eq_id,
                    "equipment_type": rec["equipment_type"],
                    "site_id": site_id, "lat": lat, "lng": lng, "ts": rec["ts"],
                })
                del self.breaches[25:]

        if out_of_bounds:
            self.alerter.breach(eq_id, rec["equipment_type"], site_id, lat, lng)

    def snapshot(self):
        with self.lock:
            return {"vehicles": list(self.vehicles.values()),
                    "breaches": self.breaches[:10],
                    "server_time": dt.datetime.now().isoformat(timespec="seconds")}


def reader_from_lines(lines, fleet):
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("Equipment ID"):
            continue
        parts = line.split(",")
        if len(parts) < len(COLS):
            continue
        fleet.update(parts[:len(COLS)])


def start_demo_breach(fleet, every, hold):
    """Optional: periodically shove one in-bounds unit past its boundary and
    HOLD it there for `hold` seconds, so the per-minute SMS repeat + red marker
    are visible on cue during a demo. The real geofence path is untouched; once
    released, the next real telemetry tick pulls the unit back inside."""
    import random

    def loop():
        while True:
            time.sleep(every)
            with fleet.lock:
                if len(fleet.held) >= 2:      # never light up the whole fleet
                    continue
                cands = [v for v in fleet.vehicles.values()
                         if v["site_id"] in SITES and not v["out_of_bounds"]
                         and v["equipment_id"] not in fleet.held]
            if not cands:
                continue
            v = random.choice(cands)
            eq_id = v["equipment_id"]
            site = SITES[v["site_id"]]
            clat, clng = site["center"]
            corner = random.choice(site["corners"])
            out_lat = clat + (corner[0] - clat) * 1.6      # push beyond a corner
            out_lng = clng + (corner[1] - clng) * 1.6
            fleet.hold_out(eq_id, out_lat, out_lng)        # stays outside
            fleet.update([eq_id, v["equipment_type"], v["site_id"],
                          v["check_in"], v["check_out"], v["engine_hrs"],
                          v["idle_hrs"], v["rental_days"], v["operator"],
                          str(out_lat), str(out_lng), v["engine_temp"],
                          v["battery_pct"], v["battery_temp"], v["fuel_level"]])
            threading.Timer(hold, fleet.release, args=(eq_id,)).start()

    threading.Thread(target=loop, daemon=True).start()


def start_ingest(fleet, source, use_stdin, interval, day_length):
    if use_stdin:
        t = threading.Thread(target=reader_from_lines,
                             args=(sys.stdin, fleet), daemon=True)
        t.start()
        return None

    cmd = [sys.executable, source, "--no-header", "--interval", str(interval)]
    if day_length > 0:
        cmd += ["--day-length", str(day_length)]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=sys.stderr,
                            text=True, bufsize=1)

    def pump():
        reader_from_lines(proc.stdout, fleet)

    threading.Thread(target=pump, daemon=True).start()
    return proc


def create_app(fleet):
    app = Flask(__name__)

    @app.route("/")
    def index():
        return Response(MAP_HTML, mimetype="text/html")

    @app.route("/api/sites")
    def api_sites():
        return jsonify(list(SITES.values()))

    @app.route("/api/positions")
    def api_positions():
        return jsonify(fleet.snapshot())

    return app


MAP_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Rental Fleet — Live Geofence Map</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  html,body{margin:0;height:100%;font-family:system-ui,Arial,sans-serif}
  #map{position:absolute;top:0;bottom:0;left:0;right:0}
  .panel{position:absolute;z-index:1000;background:rgba(20,22,28,.92);color:#eee;
         padding:10px 12px;border-radius:8px;font-size:13px;line-height:1.5;
         box-shadow:0 2px 10px rgba(0,0,0,.4)}
  #legend{top:12px;right:12px;max-width:230px}
  #alerts{bottom:12px;left:12px;max-width:340px;max-height:38vh;overflow:auto}
  .sw{display:inline-block;width:12px;height:12px;border-radius:2px;margin-right:6px;vertical-align:middle}
  h4{margin:0 0 6px;font-size:13px}
  .b{color:#ff5a5a;font-weight:600}
  .muted{color:#9aa}
  .sitelbl{background:rgba(255,255,255,.82);border:1px solid #111;border-radius:4px;
           box-shadow:0 1px 3px rgba(0,0,0,.4);color:#111;font-weight:700;
           font-size:12px;padding:1px 6px}
</style>
</head>
<body>
<div id="map"></div>
<div id="legend" class="panel">
  <h4>Fleet Geofence — LIVE</h4>
  <div><span class="sw" style="background:#f6a"></span>Construction site</div>
  <div><span class="sw" style="background:#fb0"></span>Mining site</div>
  <div><span class="sw" style="background:#2ecc71"></span>Vehicle inside boundary</div>
  <div><span class="sw" style="background:#ff3b30"></span>Vehicle OUT of bounds</div>
  <div><span class="sw" style="background:#8aa"></span>Yard / unassigned</div>
  <div class="muted" id="clock" style="margin-top:6px"></div>
</div>
<div id="alerts" class="panel">
  <h4>Boundary breaches <span class="muted">(SMS sent)</span></h4>
  <div id="breachlist" class="muted">None yet.</div>
</div>
<script>
const map = L.map('map');
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
  {maxZoom:19, attribution:'&copy; OpenStreetMap'}).addTo(map);

const KIND_COLOR = {construction:'#ff66aa', mining:'#ffbb00'};
// darker outline colours for a high-visibility boundary line
const KIND_LINE  = {construction:'#8b0038', mining:'#7a4a00'};
let markers = {};
let fitted = false;

fetch('/api/sites').then(r=>r.json()).then(sites=>{
  const all=[];
  sites.forEach(s=>{
    const latlngs = s.corners.map(c=>[c[0],c[1]]);
    const fill = KIND_COLOR[s.kind] || '#888';
    const line = KIND_LINE[s.kind]  || '#222';
    // solid dark casing underneath for contrast, then the coloured boundary
    L.polygon(latlngs,{color:'#111',weight:7,opacity:0.9,fill:false,
                       lineJoin:'round'}).addTo(map);
    L.polygon(latlngs,{color:line,weight:4,opacity:1,dashArray:'10 6',
                       fillColor:fill,fillOpacity:0.22,lineJoin:'round'})
      .addTo(map)
      .bindTooltip(`${s.site_id} · ${s.kind}`,
                   {permanent:true,direction:'center',className:'sitelbl'});
    latlngs.forEach(p=>all.push(p));
  });
  if(all.length){ map.fitBounds(all,{padding:[40,40]}); fitted=true; }
});

function vehIcon(color){
  return L.divIcon({className:'',iconSize:[16,16],
    html:`<div style="width:14px;height:14px;border-radius:50%;
      background:${color};border:2px solid #fff;box-shadow:0 0 4px rgba(0,0,0,.6)"></div>`});
}

function refresh(){
  fetch('/api/positions').then(r=>r.json()).then(d=>{
    document.getElementById('clock').textContent = 'server: ' + d.server_time;
    const seen={};
    d.vehicles.forEach(v=>{
      seen[v.equipment_id]=1;
      let color = '#8aa';               // yard / unassigned
      if(v.site_id && v.site_id.startsWith('S'))
        color = v.out_of_bounds ? '#ff3b30' : '#2ecc71';
      const pos=[v.lat,v.lng];
      const popup = `<b>${v.equipment_id}</b> — ${v.equipment_type}<br>`+
        `Site: ${v.site_id}<br>Operator: ${v.operator}<br>`+
        `Fuel: ${v.fuel_level}% · Batt: ${v.battery_pct}%<br>`+
        (v.out_of_bounds?'<span style="color:#ff3b30;font-weight:700">OUTSIDE BOUNDARY</span>':'inside boundary')+
        `<br><span style="color:#888">${v.ts}</span>`;
      if(markers[v.equipment_id]){
        markers[v.equipment_id].setLatLng(pos).setIcon(vehIcon(color)).setPopupContent(popup);
      }else{
        markers[v.equipment_id]=L.marker(pos,{icon:vehIcon(color)}).addTo(map).bindPopup(popup);
      }
    });
    Object.keys(markers).forEach(id=>{ if(!seen[id]){ map.removeLayer(markers[id]); delete markers[id]; }});

    const bl=document.getElementById('breachlist');
    if(d.breaches.length){
      bl.innerHTML = d.breaches.map(b=>
        `<div class="b">⚠ ${b.equipment_id} (${b.equipment_type}) left ${b.site_id}</div>`+
        `<div class="muted" style="margin-bottom:4px">${b.lat.toFixed(5)}, ${b.lng.toFixed(5)} · ${b.ts}</div>`
      ).join('');
    }
  }).catch(()=>{});
}
setInterval(refresh, 2000); refresh();
</script>
</body>
</html>"""


def main():
    p = argparse.ArgumentParser(description="Live geofence map for rental fleet")
    p.add_argument("--source", default="data_synth.py",
                   help="path to the telemetry generator (spawned as subprocess)")
    p.add_argument("--stdin", action="store_true",
                   help="read the live CSV feed from stdin instead of spawning --source")
    p.add_argument("--interval", type=float, default=2.0,
                   help="generator tick seconds (passed to --source)")
    p.add_argument("--day-length", type=float, default=0.0,
                   help="compress a simulated day to N seconds (passed to --source)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=5000)
    p.add_argument("--cooldown", type=float, default=60.0,
                   help="seconds between repeat SMS for a unit while it stays "
                        "out of bounds (60 = one alert per minute)")
    p.add_argument("--no-open", action="store_true", help="don't auto-open browser")
    p.add_argument("--demo-breach", type=float, default=0.0,
                   help="every N seconds, push one unit past its boundary to "
                        "demo the SMS alert on cue (0 = off / real breaches only)")
    p.add_argument("--breach-hold", type=float, default=150.0,
                   help="seconds a demo-breached unit is held outside "
                        "(with --cooldown 60 you get ~1 SMS per minute)")
    args = p.parse_args()

    fleet = Fleet(Alerter(cooldown=args.cooldown))
    start_ingest(fleet, args.source, args.stdin, args.interval, args.day_length)
    if args.demo_breach > 0:
        start_demo_breach(fleet, args.demo_breach, args.breach_hold)

    app = create_app(fleet)
    url = f"http://{args.host}:{args.port}/"
    print(f"\n  Live geofence map:  {url}\n  (Ctrl+C to stop)\n", flush=True)
    if not args.no_open:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host=args.host, port=args.port, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()