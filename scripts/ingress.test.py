"""E2E: drive the whole app through a stand-in for Home Assistant ingress.

Run with `python3 scripts/ingress.test.py`. CI runs it on every push.

The proxy below does what HA's ingress handler does — strips
/api/hassio_ingress/<token> from the path and passes the stripped prefix in
X-Ingress-Path — so the add-on is exercised exactly as it is in the sidebar.
Anything that still emits a root-absolute URL shows up here as a 404.
"""
import asyncio, os, pathlib, re, sys, tempfile
from aiohttp import web, ClientSession, CookieJar

APP_DIR = str(pathlib.Path(__file__).resolve().parent.parent
               / "advance_tools" / "rootfs" / "app")
DATA = tempfile.mkdtemp()
os.environ.update(DATA_DIR=DATA, APP_DIR=APP_DIR, DOMAIN="")
sys.path.insert(0, APP_DIR)

TOKEN = "AbC123token"
PREFIX = "/api/hassio_ingress/" + TOKEN
APP_PORT, PROXY_PORT = 18901, 18902

results = []
def check(name, cond, extra=""):
    results.append(bool(cond))
    print(("  PASS  " if cond else "  FAIL  ") + name + (" :: " + str(extra)[:200] if not cond else ""))


async def start_app():
    import main
    main.bootstrap()
    app = main.build_app()
    r = web.AppRunner(app); await r.setup()
    await web.TCPSite(r, "127.0.0.1", APP_PORT).start()
    return main


async def start_proxy():
    """Mimic HA ingress: strip the prefix, add X-Ingress-Path, relay everything."""
    async def h(req):
        path = req.path[len(PREFIX):] or "/"
        url = f"http://127.0.0.1:{APP_PORT}{path}"
        if req.query_string:
            url += "?" + req.query_string
        hdrs = {k: v for k, v in req.headers.items()
                if k.lower() not in ("host", "content-length")}
        hdrs["X-Ingress-Path"] = PREFIX
        body = await req.read()
        async with ClientSession(cookie_jar=CookieJar(unsafe=True)) as s:
            async with s.request(req.method, url, headers=hdrs, data=body or None,
                                 allow_redirects=False) as r:
                raw = await r.read()
                out = web.Response(body=raw, status=r.status)
                for k, v in r.headers.items():
                    if k.lower() in ("content-length", "transfer-encoding",
                                     "content-encoding"):
                        continue
                    out.headers.add(k, v)
                return out
    app = web.Application()
    app.router.add_route("*", "/{t:.*}", h)
    r = web.AppRunner(app); await r.setup()
    await web.TCPSite(r, "127.0.0.1", PROXY_PORT).start()


def unprefixed(html):
    """Attributes still pointing at the host root — each one is a live 404."""
    return [m.group(0) for m in
            re.finditer(r'(?:href|src|action)=["\']/(?!/|api/hassio_ingress)[^"\']*', html)]


async def main_():
    await start_app()
    await start_proxy()
    base = f"http://127.0.0.1:{PROXY_PORT}{PREFIX}"

    async with ClientSession(cookie_jar=CookieJar(unsafe=True)) as s:
        async with s.get(base + "/", allow_redirects=False) as r:
            check("GET / redirects with the ingress prefix intact",
                  r.headers.get("Location") == PREFIX + "/setup",
                  r.headers.get("Location"))

        async with s.get(base + "/setup") as r:
            html = await r.text()
            st = r.status
        check("setup page 200", st == 200, st)
        check("shim injected with the right base", 'var B = "%s"' % PREFIX in html)
        check("no root-absolute attributes left on setup", not unprefixed(html),
              unprefixed(html)[:3])

        assets = re.findall(r'(?:href|src)=["\'](' + re.escape(PREFIX) + r'/[^"\']+)', html)
        bad = []
        for a in sorted(set(assets)):
            async with s.get(f"http://127.0.0.1:{PROXY_PORT}{a}", allow_redirects=False) as r:
                if r.status not in (200, 302):
                    bad.append((a, r.status))
        check(f"all {len(set(assets))} setup assets load", not bad, bad)

        async with s.post(base + "/api/setup/password",
                          json={"username": "admin", "password": "TestPass123!"}) as r:
            check("setup password accepted", r.status == 200, await r.text())
        async with s.post(base + "/api/setup/finish", json={}) as r:
            pass
        async with s.post(base + "/api/login",
                          json={"username": "admin", "password": "TestPass123!",
                                "dashboard": "__admin__"}) as r:
            check("login through ingress", r.status == 200, await r.text())

        async with s.get(base + "/admin", allow_redirects=False) as r:
            hub = await r.text(); st = r.status
        check("hub 200 (authenticated, no redirect)", st == 200, st)
        check("no root-absolute attributes left on hub", not unprefixed(hub),
              unprefixed(hub)[:3])
        check("target=_top rewritten to _parent under ingress",
              'target="_top"' not in hub)

        async with s.get(base + "/api/tools") as r:
            tools = await r.json()
        check("tool list served", len(tools.get("tools", [])) >= 18,
              len(tools.get("tools", [])))

        pages = [t["page"] for t in tools.get("tools", [])]
        bad = []
        for p in pages:
            async with s.get(f"http://127.0.0.1:{PROXY_PORT}{PREFIX}{p}", allow_redirects=False) as r:
                txt = await r.text()
                if r.status != 200 or unprefixed(txt):
                    bad.append((p, r.status, unprefixed(txt)[:2]))
        check(f"all {len(pages)} tool pages load clean through ingress", not bad, bad)

        async with ClientSession(cookie_jar=CookieJar(unsafe=True)) as d:
            async with d.get(f"http://127.0.0.1:{APP_PORT}/setup") as r:
                direct = await r.text()
            check("direct access left untouched",
                  'var B = ""' in direct and 'href="/static/' in direct,
                  direct[:200])

    print(f"\n{sum(results)}/{len(results)} passed")
    sys.exit(0 if all(results) else 1)

asyncio.run(main_())
