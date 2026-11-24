"""Sustained-rate probe of the fancaps image CDN from one CI runner. Public images only, no credentials.

Uses the same egress logic as inf/fancaps/quick_dl.py: one shared pacer per address, several workers each with its own
curl_cffi Safari session, a random cache-busting query, and a response only counts when it is a complete JPEG.
Stops at the first HTTP 429 / Cloudflare 403 and reports how long the address lasted.
"""
import argparse, collections, json, os, platform, secrets, statistics, sys, threading, time, urllib.request
from curl_cffi import requests as cr

CDN = 'https://cdni.fancaps.net/file/fancaps-animeimages/{id}.jpg?cb={cb}'
ap = argparse.ArgumentParser()
ap.add_argument('--rate', type=float, required=True)
ap.add_argument('--seconds', type=float, default=1500)
ap.add_argument('--workers', type=int, default=3)
ap.add_argument('--offset', type=int, default=0)
ap.add_argument('--count', type=int, default=6000)
ap.add_argument('--label', default='')
a = ap.parse_args()
ids = open('ids.txt').read().split()[a.offset:a.offset + a.count]


def mem_gb():
    try:
        return round(int([l for l in open('/proc/meminfo') if l.startswith('MemTotal')][0].split()[1]) / 1024 ** 2, 1)
    except Exception:
        return None


try:
    ip = urllib.request.urlopen('https://api.ipify.org', timeout=20).read().decode()
except Exception as e:
    ip = f'unknown ({type(e).__name__})'
print(f'[{a.label}] runner: {platform.platform()} cpus={os.cpu_count()} mem={mem_gb()}GB python={platform.python_version()} '
      f'ip={ip} rate={a.rate}/s workers={a.workers} ids={len(ids)} budget={a.seconds:.0f}s', flush=True)

lock, stop = threading.Lock(), threading.Event()
state = {'next': 0.0, 'i': 0}
stats = collections.Counter()
lat, sizes, minute = [], [], collections.defaultdict(lambda: collections.Counter())
first_block = {}
T0 = time.time()


def pace():
    with lock:
        now = time.time()
        start = max(now, state['next'])
        state['next'] = start + 1.0 / a.rate
    if start > now:
        time.sleep(start - now)


def work():
    s = cr.Session(impersonate='safari17_0')
    while not stop.is_set() and time.time() - T0 < a.seconds:
        with lock:
            if state['i'] >= len(ids):
                return
            pid = ids[state['i']]
            state['i'] += 1
        pace()
        t = time.time()
        m = int((t - T0) // 60)
        try:
            r = s.get(CDN.format(id=pid, cb=secrets.token_hex(8)), timeout=60)
            body, code, h = r.content, r.status_code, {k.lower(): v for k, v in r.headers.items()}
        except Exception as e:
            with lock:
                stats['exc_' + type(e).__name__] += 1
                minute[m]['exc'] += 1
            continue
        dt = time.time() - t
        with lock:
            if code == 200 and body[:2] == b'\xff\xd8' and str(len(body)) == h.get('content-length', str(len(body))):
                stats['ok'] += 1
                minute[m]['ok'] += 1
                minute[m]['bytes'] += len(body)
                lat.append(dt)
                sizes.append(len(body))
            elif code == 429 or (code == 403 and (h.get('cf-mitigated') or b'1015' in body[:4096] or b'cloudflare' in body[:4096].lower())):
                stats['blocked'] += 1
                minute[m]['blocked'] += 1
                if 'at' not in first_block:
                    first_block.update(at=round(time.time() - T0, 1), code=code, retry_after=h.get('retry-after'),
                                       after_ok=stats['ok'])
                stop.set()
            else:
                stats[f'http_{code}'] += 1
                minute[m]['other'] += 1


def reporter():
    last = -1
    while not stop.is_set() and time.time() - T0 < a.seconds + 5:
        time.sleep(5)
        m = int((time.time() - T0) // 60)
        if m != last and m - 1 in minute:
            c = minute[m - 1]
            print(f'[{a.label}] min {m - 1:>2}: ok={c["ok"]} blocked={c["blocked"]} other={c["other"]} exc={c["exc"]} '
                  f'{c["bytes"] / 1e6:6.1f} MB', flush=True)
            last = m


threading.Thread(target=reporter, daemon=True).start()
th = [threading.Thread(target=work) for _ in range(a.workers)]
[t.start() for t in th]
[t.join() for t in th]
el = time.time() - T0
lat_s = sorted(lat)
out = dict(label=a.label, ip=ip, rate=a.rate, workers=a.workers, seconds=round(el, 1), ok=stats['ok'],
           blocked=stats['blocked'], first_block=first_block or None, other={k: v for k, v in stats.items() if k not in ('ok', 'blocked')},
           ok_per_s=round(stats['ok'] / el, 3), MBps=round(sum(sizes) / 1e6 / el, 3), mean_KB=round(statistics.mean(sizes) / 1024, 1) if sizes else None,
           lat_p50=round(lat_s[len(lat_s) // 2], 3) if lat_s else None, lat_p95=round(lat_s[int(len(lat_s) * .95)], 3) if lat_s else None)
print('RESULT ' + json.dumps(out), flush=True)
