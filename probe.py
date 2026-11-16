"""Connectivity and throughput probe for a public web site, run from a CI runner.

Reads public pages only. Prints status codes, response validity and request rates."""
import collections, os, platform, re, statistics, sys, threading, time
from concurrent.futures import ThreadPoolExecutor

import cloudscraper

BASE = 'https://fancaps.net/anime/'
UA = {'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36'}
EP_RE = re.compile(r'(episodeimages\.php\?\d+-[^"\'<> ]+)')
IMG_RE = re.compile(r'href="[^"]*?/(\d+)"[^>]*>\s*<img')


def info():
    print('python', platform.python_version(), '| cpus', os.cpu_count())
    try:
        print(open('/proc/meminfo').readline().strip())
    except Exception:
        pass


def valid(text):
    return 'Counting' in text and text.rstrip().endswith('</html>')


def fetch(s, url, params=None):
    t = time.time()
    try:
        r = s.get(url, params=params, timeout=60)
        return r.status_code, r.text, time.time() - t, r.headers
    except Exception as e:
        return repr(e)[:120], '', time.time() - t, {}


def main():
    info()
    s = cloudscraper.create_scraper()
    s.headers.update(UA)
    print('--- A. reachability')
    codes = {}
    for name, url, params in (('feed', BASE + 'recentImages.php', {'page': '1'}),
                              ('showlist', BASE + 'showList.php', {'a': '1'}),
                              ('popular', BASE + 'popular.php', {'paging': '1', 'page': '1'})):
        code, text, dt, h = fetch(s, url, params)
        codes[name] = code
        print(f'{name:<9} status={code} len={len(text)} valid={valid(text)} cf-cache={h.get("cf-cache-status")} {dt:.2f}s')
    if codes['feed'] != 200:
        print('RESULT unreachable')
        return 1
    code, text, dt, h = fetch(s, BASE + 'recentImages.php', {'page': '1'})
    eps = list(dict.fromkeys(EP_RE.findall(text)))
    for p in (2, 3):
        code, text, dt, h = fetch(s, BASE + 'recentImages.php', {'page': str(p)})
        eps += [e for e in dict.fromkeys(EP_RE.findall(text)) if e not in eps]
    eps = eps[:24]
    print('episodes found for the throughput test:', len(eps))

    print('--- B. throughput (16 workers, whole episodes, cold pages)')
    cnt = collections.Counter(); lat = []; lock = threading.Lock()
    local = threading.local()

    def crawl(href):
        if not hasattr(local, 's'):
            sc = cloudscraper.create_scraper(); sc.headers.update(UA); local.s = sc
        page = 1; n_img = 0
        while page <= 60:
            code, text, dt, h = fetch(local.s, BASE + href, {'page': str(page)})
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
        print(f'latency mean={statistics.mean(lat):.2f}s p95={sorted(lat)[int(len(lat)*0.95)-1]:.2f}s')
    ok = cnt.get('200', 0) == total and cnt.get('invalid', 0) == 0
    print('RESULT', 'ok' if ok else 'degraded')
    return 0 if ok else 2


if __name__ == '__main__':
    sys.exit(main())
