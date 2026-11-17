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


def stage_rate(strategy, workers, ids, seconds, rate):
    """Hold a global request rate; on a 429 pause everybody for Retry-After (or a backoff) and keep going."""
    print(f'--- R. {strategy} workers={workers} target={rate} img/s budget={seconds}s ids={len(ids)}')
    local = threading.local(); lock = threading.Lock()
    t0 = time.time(); state = {'next': t0, 'pause_until': 0.0, 'streak': 0, 'paused': 0.0}
    recs = []; samples429 = []; it = iter(ids); it_lock = threading.Lock()
    status = collections.Counter(); stop = threading.Event()

    def nxt():
        with it_lock:
            return next(it, None)

    def slot():
        with lock:
            now = time.time()
            at = max(state['next'], state['pause_until'], now)
            state['next'] = at + 1.0 / rate
        time.sleep(max(0.0, at - time.time()))

    def work():
        if not hasattr(local, 'f'):
            local.f = make(strategy, None)
        while not stop.is_set() and time.time() - t0 < seconds:
            i = nxt()
            if i is None:
                return
            slot()
            t = time.time()
            try:
                code, body, h = local.f(CDN.format(id=i))
            except Exception as e:
                code, body, h = 'EXC:' + type(e).__name__, b'', {}
            dt = time.time() - t
            with lock:
                recs.append((time.time() - t0, dt, code, len(body)))
                status[str(code)] += 1
                if code == 429:
                    if len(samples429) < 3:
                        samples429.append({'headers': {k: h.get(k) for k in ('retry-after', 'cf-ray', 'content-type', 'server', 'cf-mitigated', 'x-ratelimit-limit', 'x-ratelimit-remaining', 'x-ratelimit-reset', 'ratelimit') if h.get(k)},
                                           'body': snippet(body)})
                    state['streak'] += 1
                    wait = float(h.get('retry-after') or min(30, 2 ** min(state['streak'], 5)))
                    state['pause_until'] = max(state['pause_until'], time.time() + wait)
                    state['paused'] += wait
                elif code == 200:
                    state['streak'] = 0
                n = len(recs)
                if n >= 200 and n % 50 == 0 and sum(1 for r in recs[-100:] if r[2] == 429) > 50:
                    print('ABORT: more than half of the last 100 requests were 429'); stop.set()

    with ThreadPoolExecutor(workers) as ex:
        for _ in range(workers):
            ex.submit(work)
    wall = time.time() - t0
    okr = [r for r in recs if r[2] == 200]
    mb = sum(r[3] for r in okr) / 1024 ** 2
    print(f'requests={len(recs)} ok={len(okr)} 429={status.get("429", 0)} wall={wall:.0f}s -> {len(okr)/wall:.2f} img/s, {mb/wall:.2f} MiB/s | paused total {state["paused"]:.0f}s')
    print('status:', dict(status))
    print('429 samples:', json.dumps(samples429)[:700])
    per = collections.defaultdict(lambda: [0, 0])
    for ts, dt, code, n in recs:
        b = int(ts // 15); per[b][0] += (code == 200); per[b][1] += (code == 429)
    print('per 15s window (ok, 429):', [tuple(v) for _, v in sorted(per.items())])
    print('SUMMARY', json.dumps({'rate': rate, 'workers': workers, 'ok': len(okr), 'n': len(recs), 'r429': status.get('429', 0), 'img_s': round(len(okr) / wall, 2)}))


def snippet(body):
    import re
    t = re.sub(r'<script.*?</script>|<style.*?</style>', ' ', body[:6000].decode('utf-8', 'ignore'), flags=re.S)
    return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', t)).strip()[:140]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--workers', type=int, default=16); ap.add_argument('--count', type=int, default=3000)
    ap.add_argument('--offset', type=int, default=0); ap.add_argument('--seconds', type=int, default=180)
    ap.add_argument('--strategy', default='curl:safari17_0'); ap.add_argument('--referer', default='')
    ap.add_argument('--stage-a', action='store_true'); ap.add_argument('--rate', type=float, default=0)
    a = ap.parse_args()
    print('python', platform.python_version(), '| cpus', os.cpu_count(), '|', open('/proc/meminfo').readline().strip())
    s = json.load(open('sample_ids.json'))
    if a.stage_a:
        stage_a(s['gap'], s['full'])
        return 0
    ids = s['gap'][a.offset:a.offset + a.count]
    if a.rate:
        stage_rate(a.strategy, a.workers, ids, a.seconds, a.rate)
    else:
        stage_b(a.strategy, a.workers, ids, a.seconds, a.referer or None)
    return 0


if __name__ == '__main__':
    sys.exit(main())
