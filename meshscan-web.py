#!/usr/bin/env python3
"""meshscan-web: tiny read-only dashboard for /var/lib/meshscan/meshscan.db (port 8080)."""
import json, os, sqlite3, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

DB = os.environ.get("MESHSCAN_DB", "/var/lib/meshscan/meshscan.db")
PORT = int(os.environ.get("MESHSCAN_WEB_PORT", "8080"))


def q(sql, args=()):
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=5)
    db.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in db.execute(sql, args)]
    finally:
        db.close()


def api(path, qs):
    hours = float(qs.get("hours", ["24"])[0]); since = time.time() - hours * 3600
    if path == "/api/nodes":
        return q("SELECT * FROM nodes WHERE node_id NOT LIKE 'hop:%' ORDER BY last_seen DESC")
    if path == "/api/hops":
        return q("SELECT node_id, packets, last_rssi, last_snr, last_seen FROM nodes WHERE node_id LIKE 'hop:%' "
                 "ORDER BY packets DESC LIMIT 30")
    if path == "/api/stats":
        return q("SELECT config, SUM(end-start) AS secs, SUM(packets) AS packets, SUM(crc_errors) AS crc, "
                 "SUM(preambles) AS preambles, AVG(noise_dbm) AS noise FROM dwells WHERE start>? GROUP BY config "
                 "ORDER BY packets DESC", (since,))
    if path == "/api/packets":
        return q("SELECT ts, config, rssi, snr, len, crc_ok, decoded FROM packets ORDER BY ts DESC LIMIT 60")
    if path == "/api/messages":
        return q("SELECT * FROM messages ORDER BY ts DESC LIMIT 50")
    if path == "/api/summary":
        r = q("SELECT COUNT(*) n, MIN(ts) first, MAX(ts) last FROM packets")[0]
        r["nodes"] = q("SELECT COUNT(*) n FROM nodes WHERE node_id NOT LIKE 'hop:%'")[0]["n"]
        return r
    return None


PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>CardputerZero meshscan</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
body{margin:0;font:14px system-ui,sans-serif;background:#0f1115;color:#e6e6e6}
header{padding:10px 16px;background:#171a21;display:flex;gap:18px;align-items:baseline;flex-wrap:wrap}
h1{font-size:17px;margin:0} .m{color:#8b93a1} main{padding:12px 16px;display:grid;gap:14px}
#map{height:380px;border-radius:8px;background:#111} .dark{filter:invert(1) hue-rotate(180deg) brightness(.9) contrast(.9)} section{background:#171a21;border-radius:8px;padding:10px 12px;overflow:auto}
h2{font-size:14px;margin:0 0 8px} table{border-collapse:collapse;width:100%} td,th{padding:3px 8px;text-align:left;white-space:nowrap;border-bottom:1px solid #252a33}
th{color:#8b93a1;font-weight:500} .mc{color:#7ab8ff} .mt{color:#7dd87d} .bad{color:#e07a7a}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(420px,1fr));gap:14px}
</style></head><body>
<header><h1>📡 CardputerZero meshscan</h1><span class="m" id="sum"></span><span class="m">receive-only · auto-refresh 20s</span></header>
<main>
<div id="map"></div>
<section><h2>Nodes</h2><table id="nodes"></table></section>
<div class="grid">
<section><h2>Presets (last 24h)</h2><table id="stats"></table></section>
<section><h2>Directly heard relays (MeshCore path hashes)</h2><table id="hops"></table></section>
</div>
<section><h2>Messages</h2><table id="msgs"></table></section>
<section><h2>Recent packets</h2><table id="pkts"></table></section>
</main>
<script>
const map=L.map('map').setView([52.37,4.89],9);
L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:18,attribution:'© OpenStreetMap contributors',className:'dark'}).addTo(map);
const layer=L.layerGroup().addTo(map); let fitted=false;
const ago=t=>{if(!t)return'-';const s=Date.now()/1000-t;return s<90?Math.round(s)+'s':s<5400?Math.round(s/60)+'m':s<172800?Math.round(s/3600)+'h':Math.round(s/86400)+'d'};
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const f=(x,d=1)=>x==null?'-':Number(x).toFixed(d);
const j=u=>fetch(u).then(r=>r.json());
const row=(cells,cls='')=>`<tr class="${cls}">${cells.map(c=>`<td>${c}</td>`).join('')}</tr>`;
const head=h=>`<tr>${h.map(c=>`<th>${c}</th>`).join('')}</tr>`;
async function load(){
 const [s,nodes,stats,hops,msgs,pk]=await Promise.all(['summary','nodes','stats','hops','messages','packets'].map(x=>j('/api/'+x)));
 document.getElementById('sum').textContent=`${s.n} packets · ${s.nodes} nodes · last ${ago(s.last)} ago`;
 document.getElementById('nodes').innerHTML=head(['net','name','id','type','pkts','RSSI','SNR','first','last'])+
  nodes.map(n=>row([n.proto,esc(n.name||n.short_name||'?'),esc(n.node_id),esc(n.kind||n.hw||''),n.packets,f(n.last_rssi),f(n.last_snr),ago(n.first_seen),ago(n.last_seen)],n.proto=='meshcore'?'mc':'mt')).join('');
 layer.clearLayers(); const pts=[];
 nodes.filter(n=>n.lat&&n.lon).forEach(n=>{pts.push([n.lat,n.lon]);
  L.circleMarker([n.lat,n.lon],{radius:7,color:n.proto=='meshcore'?'#7ab8ff':'#7dd87d',fillOpacity:.7})
   .bindPopup(`<b>${esc(n.name)}</b><br>${n.proto} ${esc(n.kind||n.hw||'')}<br>${f(n.last_rssi)} dBm / ${f(n.last_snr)} dB<br>last ${ago(n.last_seen)} ago`).addTo(layer)});
 if(pts.length&&!fitted){map.fitBounds(pts,{padding:[30,30],maxZoom:11});fitted=true}
 document.getElementById('stats').innerHTML=head(['preset','listened','pkts','CRC err','preambles','noise'])+
  stats.map(r=>row([r.config,Math.round(r.secs/60)+'m',r.packets,r.crc,r.preambles,f(r.noise,0)+' dBm'])).join('');
 document.getElementById('hops').innerHTML=head(['relay','pkts','RSSI','SNR','last'])+
  hops.map(h=>row([esc(h.node_id.slice(4)),h.packets,f(h.last_rssi),f(h.last_snr),ago(h.last_seen)])).join('');
 document.getElementById('msgs').innerHTML=msgs.length?head(['when','net','channel','from','text'])+msgs.map(m=>row([ago(m.ts),m.proto,esc(m.channel),esc(m.src),esc(m.text)])).join(''):'<tr><td class="m">none decoded yet (only default/Public channels are decodable)</td></tr>';
 document.getElementById('pkts').innerHTML=head(['when','preset','RSSI','SNR','len','type / info'])+
  pk.map(p=>{let d={};try{d=JSON.parse(p.decoded||'{}')}catch(e){}
   const info=d.type?`${d.type} ${d.route||''} ${d.hops!=null?d.hops+' hops':''} ${esc(d.name||'')}`:d.src?`${d.port||'encrypted'} from ${d.src} ${esc(d.user?.long_name||d.text||'')}`:'';
   return row([ago(p.ts),p.config,f(p.rssi),f(p.snr),p.len,p.crc_ok?info:'<span class="bad">CRC error</span>'])}).join('');
}
load(); setInterval(load,20000);
</script></body></html>"""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def do_GET(self):
        u = urlparse(self.path)
        try:
            if u.path in ("/", "/index.html"):
                body, ctype = PAGE.encode(), "text/html; charset=utf-8"
            else:
                data = api(u.path, parse_qs(u.query))
                if data is None:
                    self.send_error(404); return
                body, ctype = json.dumps(data).encode(), "application/json"
        except Exception as e:
            body, ctype = json.dumps({"error": str(e)}).encode(), "application/json"
            self.send_response(500)
        else:
            self.send_response(200)
        self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
