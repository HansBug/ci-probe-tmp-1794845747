"""Read-only check of how some sites answer a GitHub-hosted runner. A handful of single requests, no credentials."""
import json, os, platform, sys, time, urllib.request
import requests
from curl_cffi import requests as cr
label = sys.argv[1]
try: ip = urllib.request.urlopen('https://api.ipify.org', timeout=20).read().decode()
except Exception as e: ip = f'unknown ({type(e).__name__})'
print(f'[{label}] runner {platform.platform()} ip={ip}', flush=True)
CHROME = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36'
def shape(r):
    try:
        x = r.json(); return f'json={type(x).__name__}' + (f' keys={list(x)[:3]}' if isinstance(x, dict) else f' len={len(x)}')
    except Exception: return 'not-json'
def run(name, fn):
    t = time.time()
    try:
        r = fn(); print(f'[{label}] {name:<46} HTTP {r.status_code} {len(r.content):>9,} B in {time.time() - t:5.1f}s {shape(r)} | {r.text[:60]!r}', flush=True)
    except Exception as e:
        print(f'[{label}] {name:<46} {type(e).__name__} after {time.time() - t:5.1f}s: {str(e)[:110]}', flush=True)
for url in ('https://yande.re/tag.json', 'https://yande.re/tag.json?limit=0'):
    run(f'requests default UA        {url[17:]}', lambda: requests.get(url, timeout=(10, 70)))
    run(f'requests chrome UA         {url[17:]}', lambda: requests.get(url, headers={'User-Agent': CHROME}, timeout=(10, 70)))
    run(f'curl_cffi safari17_0       {url[17:]}', lambda: cr.Session(impersonate='safari17_0').get(url, timeout=70))
run('yande.re posts.json?limit=1 (requests, chrome UA)', lambda: requests.get('https://yande.re/post.json?limit=1', headers={'User-Agent': CHROME}, timeout=(10, 70)))
run('e621 posts.json?limit=1 (requests, workflow UA)', lambda: requests.get('https://e621.net/posts.json?limit=1', headers={'User-Agent': 'Mozilla/5.0 (Windows NT 6.3; rv:36.0) Gecko/20100101 Firefox/1.5'}, timeout=(10, 40)))
run('e621 posts.json?limit=1 (curl_cffi safari)', lambda: cr.Session(impersonate='safari17_0').get('https://e621.net/posts.json?limit=1', timeout=40))
run('e6ai www.e6ai.net/posts.json (no redirects)', lambda: cr.Session(impersonate='safari17_0').get('https://www.e6ai.net/posts.json?limit=1', timeout=30, allow_redirects=False))
run('e6ai e6ai.net/posts.json', lambda: cr.Session(impersonate='safari17_0').get('https://e6ai.net/posts.json?limit=1', timeout=30))
