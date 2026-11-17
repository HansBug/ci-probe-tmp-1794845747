"""Throughput and stability probe of an image CDN, run from a CI runner. Downloads public images only."""
import argparse, collections, io, json, os, platform, random, statistics, sys, threading, time
from concurrent.futures import ThreadPoolExecutor

CDN = 'https://cdni.fancaps.net/file/fancaps-animeimages/{id}.jpg'
ALT = 'https://ancdn.fancaps.net/{id}.jpg'
REFERER = 'https://fancaps.net/'
MAGIC = [(b'\xff\xd8\xff', 'jpeg'), (b'\x89PNG', 'png'), (b'RIFF', 'webp'), (b'GIF8', 'gif')]


def make(strategy, referer=None):
    kind, _, arg = strategy.partition(':')
    headers = {'Referer': referer} if referer else {}
    if kind == 'curl':
        from curl_cffi import requests as cr
        s = cr.Session(impersonate=arg)
        return lambda u: (lambda r: (r.status_code, r.content, r.headers))(s.get(u, headers=headers, timeout=60))
    if kind == 'requests':
        import requests
        s = requests.Session(); s.headers.update({'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) Chrome/120.0'})
        return lambda u: (lambda r: (r.status_code, r.content, r.headers))(s.get(u, headers=headers, timeout=60))
    if kind == 'cloudscraper':
        import cloudscraper
        s = cloudscraper.create_scraper()
        return lambda u: (lambda r: (r.status_code, r.content, r.headers))(s.get(u, headers=headers, timeout=60))
    raise ValueError(strategy)


def sniff(body):
    for sig, name in MAGIC:
        if body.startswith(sig):
            return name
    return 'html' if body[:1] == b'<' else 'other'


def stage_a(ids_gap, ids_full):
    print('--- A. reachability per strategy, host and referer (3 gap ids + 1 old id each)')
    out = {}
    for st in ('curl:safari17_0', 'curl:firefox135', 'curl:edge101', 'requests', 'cloudscraper'):
        for host, tpl in (('cdni', CDN), ('ancdn', ALT)):
            for ref in (None, REFERER):
                try:
                    f = make(st, ref)
                    res = []
                    for i in ids_gap[:3] + ids_full[:1]:
                        code, body, h = f(tpl.format(id=i))
                        res.append((code, len(body), sniff(body)))
                    print(f'{st:<17} {host:<6} referer={"yes" if ref else "no":<3} -> {res}')
                    out[(st, host, bool(ref))] = res
                except Exception as e:
                    print(f'{st:<17} {host:<6} referer={"yes" if ref else "no":<3} -> ERROR {repr(e)[:90]}')
    return out


def stage_b(strategy, workers, ids, seconds, referer):
    print(f'--- B. {strategy} workers={workers} ids={len(ids)} budget={seconds}s referer={bool(referer)}')
    local = threading.local(); lock = threading.Lock()
    recs = []; stop = threading.Event(); t0 = time.time()
    status = collections.Counter(); kinds = collections.Counter(); undecodable = [0, 0]
    try:
        from PIL import Image
    except Exception:
        Image = None
    it = iter(ids); it_lock = threading.Lock()

    def nxt():
        with it_lock:
            return next(it, None)

    def work():
        if not hasattr(local, 'f'):
            local.f = make(strategy, referer)
        rnd = random.Random(threading.get_ident())
        while not stop.is_set() and time.time() - t0 < seconds:
            i = nxt()
            if i is None:
                return
            t = time.time()
            try:
                code, body, h = local.f(CDN.format(id=i))
            except Exception as e:
                code, body, h = 'EXC:' + type(e).__name__, b'', {}
            dt = time.time() - t
            k = sniff(body) if body else '-'
            ok_decode = None
            if code == 200 and Image is not None and rnd.random() < 0.08:
                try:
                    Image.open(io.BytesIO(body)).load(); ok_decode = True
                except Exception:
                    ok_decode = False
            with lock:
                recs.append((time.time() - t0, dt, code, len(body), k))
                status[str(code)] += 1; kinds[k] += 1
                if ok_decode is not None:
                    undecodable[0] += 1; undecodable[1] += (not ok_decode)
                done = len(recs)
                if done >= 300 and done % 100 == 0:        # protect the site: stop on a failure surge
                    last = recs[-100:]
                    if sum(1 for r in last if r[2] != 200) > 40:
                        print('ABORT: more than 40 of the last 100 requests failed'); stop.set()

    with ThreadPoolExecutor(workers) as ex:
        for _ in range(workers):
            ex.submit(work)
    wall = time.time() - t0
    okr = [r for r in recs if r[2] == 200]
    mb = sum(r[3] for r in okr) / 1024 ** 2
    print(f'requests={len(recs)} ok={len(okr)} wall={wall:.1f}s -> {len(okr)/wall:.1f} img/s, {mb/wall:.1f} MiB/s ({mb:.0f} MiB)')
    print('status:', dict(status), '| sniffed:', dict(kinds), '| decode check (checked, bad):', tuple(undecodable))
    if okr:
        sizes = sorted(r[3] for r in okr); lat = sorted(r[1] for r in okr)
        print(f'size KB mean={statistics.mean(sizes)/1024:.0f} median={sizes[len(sizes)//2]/1024:.0f} p90={sizes[int(len(sizes)*.9)]/1024:.0f} max={sizes[-1]/1024:.0f}')
        print(f'latency s mean={statistics.mean(lat):.2f} p50={lat[len(lat)//2]:.2f} p95={lat[int(len(lat)*.95)]:.2f}')
    per = collections.defaultdict(lambda: [0, 0, 0])
    for ts, dt, code, n, k in recs:
        b = int(ts // 30); per[b][0] += 1; per[b][1] += (code == 200) * n; per[b][2] += (code != 200)
    print('per 30s window (img/s, MiB/s, failures):', [(round(v[0] / 30, 1), round(v[1] / 1024 ** 2 / 30, 1), v[2]) for _, v in sorted(per.items())])
    bad = [(r[2]) for r in recs if r[2] != 200][:10]
    print('first non-200 codes:', bad)
    print('SUMMARY', json.dumps({'strategy': strategy, 'workers': workers, 'img_s': round(len(okr) / wall, 1),
                                 'mib_s': round(mb / wall, 1), 'ok': len(okr), 'n': len(recs),
                                 'status': dict(status)}))
    return recs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--workers', type=int, default=16); ap.add_argument('--count', type=int, default=3000)
    ap.add_argument('--offset', type=int, default=0); ap.add_argument('--seconds', type=int, default=180)
    ap.add_argument('--strategy', default='curl:safari17_0'); ap.add_argument('--referer', default='')
    ap.add_argument('--stage-a', action='store_true')
    a = ap.parse_args()
    print('python', platform.python_version(), '| cpus', os.cpu_count(), '|', open('/proc/meminfo').readline().strip())
    s = json.load(open('sample_ids.json'))
    if a.stage_a:
        stage_a(s['gap'], s['full'])
        return 0
    stage_b(a.strategy, a.workers, s['gap'][a.offset:a.offset + a.count], a.seconds, a.referer or None)
    return 0


if __name__ == '__main__':
    sys.exit(main())
