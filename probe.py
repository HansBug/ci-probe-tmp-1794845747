"""Connectivity and throughput probe for a public web site, run from a CI runner.

Reads public pages only. Prints which client strategies get through, then the request rate of the best one."""
import collections, os, platform, re, statistics, sys, threading, time, typing
from concurrent.futures import ThreadPoolExecutor

BASE = 'https://fancaps.net/anime/'
UA = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36'
EP_RE = re.compile(r'(episodeimages\.php\?\d+-[^"\'<> ]+)')
IMG_RE = re.compile(r'href="[^"]*?/(\d+)"[^>]*>\s*<img')


def valid(text):
    return 'Counting' in text and text.rstrip().endswith('</html>')


def snippet(text):
    t = re.sub(r'<script.*?</script>|<style.*?</style>', ' ', text, flags=re.S)
    t = re.sub(r'<[^>]+>', ' ', t)
    return re.sub(r'\s+', ' ', t).strip()[:160]


def make(strategy):
    """Return a callable (url, params) -> (status, text, headers)."""
    kind, _, arg = strategy.partition(':')
    if kind == 'cloudscraper':
        import cloudscraper
        s = cloudscraper.create_scraper(); s.headers.update({'User-Agent': UA})
        return lambda u, p: (lambda r: (r.status_code, r.text, r.headers))(s.get(u, params=p, timeout=60))
    if kind == 'requests':
        import requests
        s = requests.Session(); s.headers.update({'User-Agent': UA})
        return lambda u, p: (lambda r: (r.status_code, r.text, r.headers))(s.get(u, params=p, timeout=60))
    if kind == 'curl':
        from curl_cffi import requests as cr
        s = cr.Session(impersonate=arg)
        return lambda u, p: (lambda r: (r.status_code, r.text, r.headers))(s.get(u, params=p, timeout=60))
    raise ValueError(strategy)


def supported():
    from curl_cffi.requests.impersonate import BrowserTypeLiteral
    return set(typing.get_args(BrowserTypeLiteral))


def main():
    print('python', platform.python_version(), '| cpus', os.cpu_count(), '|', open('/proc/meminfo').readline().strip())
    import curl_cffi
    print('curl_cffi', curl_cffi.__version__)
    sup = supported()
    wanted = ['safari', 'safari17_0', 'safari15_5', 'safari18_0', 'safari_ios', 'firefox', 'firefox133', 'firefox135',
              'chrome', 'chrome120', 'chrome124', 'chrome131', 'edge101', 'edge99']
    strategies = ['cloudscraper', 'requests'] + ['curl:' + w for w in wanted if w in sup]
    print('--- A. reachability of the feed page per client strategy')
    winners = []
    for st in strategies:
        try:
            f = make(st)
            code, text, h = f(BASE + 'recentImages.php', {'page': '1'})
            ok = code == 200 and valid(text)
            extra = '' if ok else f' | server={h.get("server")} cf-mitigated={h.get("cf-mitigated")} body="{snippet(text)}"'
            print(f'{st:<22} status={code} valid={ok}{extra}')
            if ok:
                winners.append(st)
        except Exception as e:
            print(f'{st:<22} ERROR {repr(e)[:100]}')
    print('WINNERS', winners)
    if not winners:
        print('RESULT unreachable')
        return 1
    best = winners[0]
    f = make(best)
    eps = []
    for p in (1, 2, 3):
        code, text, h = f(BASE + 'recentImages.php', {'page': str(p)})
        eps += [e for e in dict.fromkeys(EP_RE.findall(text)) if e not in eps]
    eps = eps[:24]
    print(f'--- B. throughput with {best}: 16 workers, {len(eps)} whole episodes, cold pages')
    cnt = collections.Counter(); lat = []; lock = threading.Lock(); local = threading.local()

    def crawl(href):
        if not hasattr(local, 'f'):
            local.f = make(best)
        page = 1; n_img = 0
        while page <= 60:
            t = time.time()
            try:
                code, text, h = local.f(BASE + href, {'page': str(page)})
            except Exception as e:
                code, text = repr(e)[:60], ''
            dt = time.time() - t
            with lock:
                cnt[str(code)] += 1; lat.append(dt)
                cnt['valid' if (code == 200 and valid(text)) else 'invalid'] += 1
            if code != 200 or not valid(text):
                break
            ids = set(IMG_RE.findall(text))
            if not ids:
                break
            n_img += len(ids); page += 1
        return n_img

    t0 = time.time()
    with ThreadPoolExecutor(16) as ex:
        imgs = list(ex.map(crawl, eps))
    dt = time.time() - t0
    total = sum(v for k, v in cnt.items() if k not in ('valid', 'invalid'))
    print(f'requests={total} in {dt:.1f}s -> {total/dt:.1f} req/s | image-links seen={sum(imgs)}')
    print('status/validity counts:', dict(cnt))
    if lat:
        print(f'latency mean={statistics.mean(lat):.2f}s p95={sorted(lat)[max(0, int(len(lat)*0.95)-1)]:.2f}s')
    ok = cnt.get('200', 0) == total and cnt.get('invalid', 0) == 0
    print('RESULT', 'ok' if ok else 'degraded')
    return 0 if ok else 2


if __name__ == '__main__':
    sys.exit(main())
