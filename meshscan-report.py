#!/usr/bin/env python3
"""meshscan-report: summarise /var/lib/meshscan/meshscan.db"""
import os, sqlite3, sys, time
db = sqlite3.connect(os.environ.get("MESHSCAN_DB", "/var/lib/meshscan/meshscan.db"))
hours = float(sys.argv[1]) if len(sys.argv) > 1 else 24
since = time.time() - hours * 3600
fmt = lambda t: time.strftime("%m-%d %H:%M", time.localtime(t)) if t else "-"

print(f"== Per config, last {hours:g}h")
print(f"{'config':26} {'listened':>9} {'pkts':>5} {'crcErr':>6} {'hdrs':>5} {'preamb':>6} {'noise':>6}")
for r in db.execute("SELECT config, SUM(end-start), SUM(packets), SUM(crc_errors), SUM(headers), SUM(preambles), "
                    "AVG(noise_dbm) FROM dwells WHERE start>? GROUP BY config ORDER BY 3 DESC", (since,)):
    print(f"{r[0]:26} {r[1]/60:8.0f}m {r[2]:5} {r[3]:6} {r[4]:5} {r[5]:6} {r[6] or 0:6.0f}")

print("\n== Nodes")
for r in db.execute("SELECT proto,node_id,COALESCE(name,''),COALESCE(short_name,''),COALESCE(kind,hw,''),packets,"
                    "last_rssi,last_snr,first_seen,last_seen,lat,lon FROM nodes ORDER BY last_seen DESC"):
    pos = f" @{r[10]:.4f},{r[11]:.4f}" if r[10] else ""
    print(f"{r[0]:10} {r[1]:18} {r[2][:24]:24} {r[3]:5} {r[4][:14]:14} n={r[5]:<4} {r[6]:6.1f}dBm {r[7]:5.1f}dB "
          f"first {fmt(r[8])} last {fmt(r[9])}{pos}")

print("\n== Recent messages")
for r in db.execute("SELECT ts,proto,channel,src,text FROM messages ORDER BY ts DESC LIMIT 15"):
    print(f"{fmt(r[0])} {r[1]:10} [{r[2]}] {r[3] or ''}: {r[4]}")

print("\n== Recent packets")
for r in db.execute("SELECT ts,config,rssi,snr,len,crc_ok,substr(COALESCE(decoded,raw),1,140) FROM packets ORDER BY ts DESC LIMIT 15"):
    print(f"{fmt(r[0])} {r[1]:24} {r[2]:6.1f} {r[3]:5.1f} {r[4]:3}B {'ok ' if r[5] else 'CRC'} {r[6]}")
