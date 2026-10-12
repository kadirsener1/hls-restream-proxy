#!/usr/bin/env python3
"""
HLS Restream Proxy v5 - Railway Edition
- Cloudflare bypass (curl_cffi)
- Dinamik JS player için Playwright (reklam engelleme + genişletilmiş bekleme)
- Alt playlist (chunklist) rewrite
- Range başlığı iletimi
- Sabit /channel/<slug> URL
- Arka planda token yenileme
"""

import os
import re
import json
import time
import threading
import urllib.parse
import urllib.request
import http.server
import socketserver
from datetime import datetime

# Cloudflare bypass
try:
    from curl_cffi import requests as cffi_requests
    HAS_CFFI = True
except ImportError:
    HAS_CFFI = False
    print("[!] curl_cffi yuklu degil, urllib kullanilacak")

# Playwright (dinamik JS için)
try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False
    print("[!] playwright yuklu degil, JS player calismayabilir")

# ============ AYARLAR ============
PORT = int(os.environ.get("PORT", 8080))
CACHE_TTL = int(os.environ.get("CACHE_TTL", 3600))
REFRESH_INTERVAL = int(os.environ.get("REFRESH_INTERVAL", 14400))
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64; rv:149.0) Gecko/20100101 Firefox/149.0"

CHANNELS = {}

# ============ KANAL YÜKLEME ============
def load_channels():
    global CHANNELS
    config_path = os.environ.get("CHANNELS_CONF", "channels.conf")
    if not os.path.exists(config_path):
        print(f"[!] {config_path} bulunamadi!")
        return

    with open(config_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("|")
            if len(parts) < 6:
                continue
            slug = parts[0].strip()
            CHANNELS[slug] = {
                "name": parts[1].strip() if len(parts) > 1 else slug,
                "chno": parts[2].strip() if len(parts) > 2 else "",
                "logo": parts[3].strip() if len(parts) > 3 else "",
                "group": parts[4].strip() if len(parts) > 4 else "General",
                "source_url": parts[5].strip(),
                "mode": parts[6].strip() if len(parts) > 6 else "direct",
                "referer": parts[7].strip() if len(parts) > 7 else "",
                "m3u8_url": None,
                "last_refresh": None,
            }
    print(f"[+] {len(CHANNELS)} kanal yuklendi.")

# ============ HTTP GET (Cloudflare bypass) ============
def fetch_url(url, referer=None, timeout=20, extra_headers=None):
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "identity",
    }
    if referer:
        headers["Referer"] = referer
        try:
            p = urllib.parse.urlparse(referer)
            headers["Origin"] = f"{p.scheme}://{p.netloc}"
        except Exception:
            pass
    if extra_headers:
        headers.update(extra_headers)

    if HAS_CFFI:
        try:
            session = cffi_requests.Session(impersonate="chrome124")
            resp = session.get(url, headers=headers, timeout=timeout, allow_redirects=True)
            return resp.text
        except Exception as e:
            print(f"[!] curl_cffi hatasi ({url[:80]}): {e}")

    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="ignore")
    except Exception as e:
        print(f"[!] urllib hatasi ({url[:80]}): {e}")
        return None

# ============ M3U8 ÇIKARMA ============
def extract_m3u8_from_html(html):
    if not html:
        return None

    html_clean = re.sub(r'<!--.*?-->', '', html, flags=re.DOTALL)

    patterns = [
        r'''file\s*:\s*["']([^"']+\.m3u8[^"']*)["']''',
        r'''source\s*:\s*["']([^"']+\.m3u8[^"']*)["']''',
        r'''src\s*:\s*["']([^"']+\.m3u8[^"']*)["']''',
        r'''url\s*:\s*["']([^"']+\.m3u8[^"']*)["']''',
        r'''["'](https?://[^"'\s<>\\]+\.m3u8[^"'\s<>\\]*)["']''',
        r'''(https?://[^\s"'<>\\]+\.m3u8[^\s"'<>\\]*)''',
        r'''["'](//[^"'\s<>\\]+\.m3u8[^"'\s<>\\]*)["']''',
    ]

    for pattern in patterns:
        m = re.search(pattern, html_clean, re.IGNORECASE)
        if m:
            url = m.group(1).strip()
            if url.startswith("//"):
                url = "https:" + url
            if len(url) > 15:
                return url
    return None

def extract_iframes(html):
    if not html:
        return []
    iframes = re.findall(r'<iframe[^>]*src\s*=\s*["\']([^"\']+)["\']', html, re.IGNORECASE)
    skip = ["ad.html", "ads.", "doubleclick", "googlesyndication", "adservice"]
    result = []
    for src in iframes:
        if any(s in src.lower() for s in skip):
            print(f"[*] Reklam iframe atlandi: {src[:80]}")
            continue
        result.append(src)
    return result

# ============ PLAYWRIGHT İLE M3U8 YAKALAMA ============
def resolve_m3u8_with_playwright(channel):
    """Gerçek tarayıcı ile JS player'dan m3u8 yakala"""
    if not HAS_PLAYWRIGHT:
        return None

    source_url = channel["source_url"]
    referer = channel["referer"] or source_url
    captured = {"m3u8": None}

    def handle_response(response):
        try:
            url = response.url
            # Tüm ilgili istekleri logla (debug)
            if any(ext in url for ext in [".m3u8", ".mpd", ".ts", "playlist", "manifest"]):
                print(f"[PW-NET] {response.status} {url[:150]}")
            if ".m3u8" in url and captured["m3u8"] is None:
                captured["m3u8"] = url
                print(f"[+] Playwright m3u8 yakaladi: {url[:150]}")
        except Exception:
            pass

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--disable-blink-features=AutomationControlled",
                ]
            )
            context = browser.new_context(
                user_agent=USER_AGENT,
                viewport={"width": 1920, "height": 1080},
                locale="en-US",
            )

            # Reklamları engelle
            ad_domains = [
                "wpnxiswpuyrfn.icu", "vbcojhroxkoaf.online",
                "doubleclick", "googlesyndication", "adsystem",
                "adservice", "ads.", "ad.html"
            ]
            context.route("**/*", lambda route: route.abort()
                if any(ad in route.request.url for ad in ad_domains)
                else route.continue_())

            page = context.new_page()
            page.on("response", handle_response)

            print(f"[*] Playwright sayfa aciliyor: {source_url}")
            page.goto(source_url, wait_until="load", timeout=60000)

            # m3u8 yakalanana kadar bekle (max 30 sn)
            for i in range(30):
                if captured["m3u8"]:
                    break
                page.wait_for_timeout(1000)

            browser.close()
    except Exception as e:
        print(f"[!] Playwright hatasi: {e}")

    return captured["m3u8"]

# ============ M3U8 ÇÖZÜMLEME ============
def resolve_m3u8(channel):
    source_url = channel["source_url"]
    referer = channel["referer"] or source_url

    print(f"[*] {channel['name']} icin m3u8 cozumleniyor: {source_url}")

    # 1) Önce curl_cffi ile dene (hızlı yol)
    html = fetch_url(source_url, referer=referer)
    if html:
        print(f"[DEBUG] HTML uzunluk: {len(html)}")

        m3u8 = extract_m3u8_from_html(html)
        if m3u8:
            print(f"[+] m3u8 bulundu (curl): {m3u8[:120]}")
            return m3u8

        iframes = extract_iframes(html)
        print(f"[*] Bulunan iframe sayisi (reklam haric): {len(iframes)}")

        for iframe_src in iframes:
            if not iframe_src.startswith("http"):
                iframe_src = urllib.parse.urljoin(source_url, iframe_src)
            print(f"[*] iframe deneniyor: {iframe_src}")
            iframe_html = fetch_url(iframe_src, referer=source_url)
            if iframe_html:
                m3u8 = extract_m3u8_from_html(iframe_html)
                if m3u8:
                    print(f"[+] iframe icinde m3u8: {m3u8[:120]}")
                    return m3u8

    # 2) Playwright ile dene (JS player için)
    print(f"[*] curl ile bulunamadi, Playwright deneniyor: {channel['name']}")
    m3u8 = resolve_m3u8_with_playwright(channel)
    if m3u8:
        return m3u8

    print(f"[!] m3u8 bulunamadi: {channel['name']}")
    return None

# ============ PROXY HANDLER ============
class ProxyHandler(http.server.BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        try:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] {args[0]}")
        except Exception:
            pass

    def _send_error_safe(self, code, message):
        try:
            self.send_response(code)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(message.encode("utf-8"))
        except Exception:
            pass

    def _fetch_binary(self, url, referer=None, timeout=20, range_header=None):
        """Binary içerik çek (segment veya alt playlist)"""
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "*/*",
            "Accept-Encoding": "identity",
        }
        if referer:
            headers["Referer"] = referer
            try:
                p = urllib.parse.urlparse(referer)
                headers["Origin"] = f"{p.scheme}://{p.netloc}"
            except Exception:
                pass
        if range_header:
            headers["Range"] = range_header

        if HAS_CFFI:
            try:
                session = cffi_requests.Session(impersonate="chrome124")
                r = session.get(url, headers=headers, timeout=timeout)
                return r.content, r.headers.get("Content-Type", "application/octet-stream"), r.status_code
            except Exception as e:
                print(f"[!] cffi binary hatasi: {e}")

        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read(), resp.headers.get("Content-Type", "application/octet-stream"), resp.status
        except Exception as e:
            print(f"[!] urllib binary hatasi: {e}")
            return None, None, None

    def do_GET(self):
        try:
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
        except Exception:
            self._send_error_safe(400, "Bad request")
            return

        # ---------- /channel/<slug> ----------
        if path.startswith("/channel/"):
            slug = urllib.parse.unquote(path[len("/channel/"):].strip("/"))

            if slug not in CHANNELS:
                self._send_error_safe(404, f"Kanal bulunamadi: {slug}")
                return

            channel = CHANNELS[slug]
            now = datetime.now()

            if (channel["m3u8_url"] and channel["last_refresh"] and
                (now - channel["last_refresh"]).total_seconds() < CACHE_TTL):
                m3u8_url = channel["m3u8_url"]
                print(f"[+] Cache: {slug}")
            else:
                m3u8_url = resolve_m3u8(channel)
                if m3u8_url:
                    channel["m3u8_url"] = m3u8_url
                    channel["last_refresh"] = now
                else:
                    m3u8_url = channel["m3u8_url"]

            if not m3u8_url:
                self._send_error_safe(503, "Yayin su anda alinamiyor")
                return

            m3u8_content = fetch_url(m3u8_url, referer=channel.get("referer") or channel["source_url"])
            if not m3u8_content:
                self._send_error_safe(502, "Upstream m3u8 alinamadi")
                return

            base_url = m3u8_url.rsplit("/", 1)[0]
            ref = urllib.parse.quote(channel.get("referer") or channel["source_url"], safe="")

            lines = m3u8_content.split("\n")
            rewritten = []
            for line in lines:
                s = line.strip()
                if s and not s.startswith("#"):
                    if not s.startswith("http"):
                        s = f"{base_url}/{s}"
                    proxy_seg = f"/segment?url={urllib.parse.quote(s, safe='')}&ref={ref}"
                    rewritten.append(proxy_seg)
                else:
                    rewritten.append(line)

            content = "\n".join(rewritten).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.apple.mpegurl")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(content)

        # ---------- /segment (segment veya alt playlist) ----------
        elif path == "/segment":
            query = urllib.parse.parse_qs(parsed.query)
            seg_url = query.get("url", [None])[0]
            ref = query.get("ref", [None])[0]
            if ref:
                ref = urllib.parse.unquote(ref)
            if seg_url:
                seg_url = urllib.parse.unquote(seg_url)

            if not seg_url:
                self._send_error_safe(400, "url parametresi eksik")
                return

            # Tivimate'ten gelen Range başlığını al
            range_header = self.headers.get("Range")

            # DEBUG log
            print(f"[SEG] {seg_url[:100]} | Range: {range_header} | Ref: {ref}")

            data, content_type, status_code = self._fetch_binary(
                seg_url, referer=ref, range_header=range_header
            )
            if data is None:
                self._send_error_safe(502, "Segment alinamadi")
                return

            # Alt playlist (chunklist) ise rewrite et
            is_m3u8 = (
                "mpegurl" in (content_type or "").lower()
                or seg_url.split("?")[0].endswith(".m3u8")
            )

            if is_m3u8:
                text = data.decode("utf-8", errors="ignore")
                base_url = seg_url.rsplit("/", 1)[0]
                ref_enc = urllib.parse.quote(ref or "", safe="")
                lines = text.split("\n")
                rewritten = []
                for line in lines:
                    s = line.strip()
                    if s and not s.startswith("#"):
                        if not s.startswith("http"):
                            s = f"{base_url}/{s}"
                        proxy_seg = f"/segment?url={urllib.parse.quote(s, safe='')}&ref={ref_enc}"
                        rewritten.append(proxy_seg)
                    else:
                        rewritten.append(line)
                data = "\n".join(rewritten).encode("utf-8")
                content_type = "application/vnd.apple.mpegurl"

            # Yanıt gönder (Range varsa 206)
            resp_status = 206 if range_header else (status_code if status_code and status_code < 400 else 200)
            self.send_response(resp_status)
            self.send_header("Content-Type", content_type or "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Accept-Ranges", "bytes")
            if range_header:
                self.send_header("Content-Range", f"bytes 0-{len(data)-1}/{len(data)}")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)

        # ---------- /health ----------
        elif path == "/health":
            body = json.dumps({
                "status": "ok",
                "channels": {k: bool(v["m3u8_url"]) for k, v in CHANNELS.items()},
                "time": datetime.now().isoformat()
            }, ensure_ascii=True).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        # ---------- / veya /playlist.m3u ----------
        elif path == "/" or path == "/playlist.m3u":
            host = self.headers.get("Host", "localhost")
            scheme = "https" if self.headers.get("X-Forwarded-Proto") == "https" else "http"
            lines = ["#EXTM3U"]
            for slug, ch in CHANNELS.items():
                lines.append(
                    f'#EXTINF:-1 tvg-id="{slug}" tvg-name="{ch["name"]}" '
                    f'tvg-logo="{ch["logo"]}" group-title="{ch["group"]}",{ch["name"]}'
                )
                lines.append(f'{scheme}://{host}/channel/{slug}')
            body = "\n".join(lines).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "audio/x-mpegurl")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        else:
            self._send_error_safe(404, "Not found")

# ============ ARKA PLAN YENİLEYİCİ ============
def background_refresher():
    while True:
        time.sleep(REFRESH_INTERVAL)
        print(f"\n[REFRESH] Basliyor: {datetime.now()}")
        for slug, channel in CHANNELS.items():
            try:
                m3u8_url = resolve_m3u8(channel)
                if m3u8_url:
                    channel["m3u8_url"] = m3u8_url
                    channel["last_refresh"] = datetime.now()
                    print(f"[OK] {channel['name']} yenilendi")
                else:
                    print(f"[!] {channel['name']} yenilenemedi")
            except Exception as e:
                print(f"[!] {channel['name']} hata: {e}")

# ============ MAIN ============
def main():
    print("=" * 60)
    print("  HLS Restream Proxy v5 - Railway")
    print("=" * 60)

    load_channels()
    if not CHANNELS:
        print("[!] Kanal yok!")
        return

    print("\n[*] Baslangic cozumlemesi...")
    for slug, channel in CHANNELS.items():
        m3u8_url = resolve_m3u8(channel)
        if m3u8_url:
            channel["m3u8_url"] = m3u8_url
            channel["last_refresh"] = datetime.now()

    t = threading.Thread(target=background_refresher, daemon=True)
    t.start()

    print(f"\n[+] Sunucu port {PORT}'de basliyor...")
    for slug, ch in CHANNELS.items():
        status = "OK" if ch["m3u8_url"] else "FAIL"
        print(f"    [{status}] /channel/{slug}")
    print("=" * 60)

    socketserver.ThreadingTCPServer.allow_reuse_address = True
    with socketserver.ThreadingTCPServer(("0.0.0.0", PORT), ProxyHandler) as httpd:
        httpd.serve_forever()

if __name__ == "__main__":
    main()
