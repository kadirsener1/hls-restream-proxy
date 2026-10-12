#!/usr/bin/env python3
"""
HLS Restream Proxy - Railway Edition (v2)
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

# ============ HTTP GET ============
def fetch_url(url, referer=None, timeout=15, extra_headers=None):
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

    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
            return data.decode("utf-8", errors="ignore")
    except Exception as e:
        print(f"[!] fetch_url hatasi ({url[:80]}): {e}")
        return None

# ============ M3U8 ÇIKARMA (GENİŞLETİLMİŞ) ============
def extract_m3u8_from_html(html):
    if not html:
        return None

    # Yorumları temizle
    html_clean = re.sub(r'<!--.*?-->', '', html, flags=re.DOTALL)

    patterns = [
        r'''file\s*:\s*["']([^"']+\.m3u8[^"']*)["']''',
        r'''source\s*:\s*["']([^"']+\.m3u8[^"']*)["']''',
        r'''src\s*:\s*["']([^"']+\.m3u8[^"']*)["']''',
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

def extract_all_scripts(html):
    """HTML içindeki tüm script src ve inline script'leri döndür"""
    scripts = []
    for m in re.finditer(r'<script[^>]*src\s*=\s*["\']([^"\']+)["\']', html, re.IGNORECASE):
        scripts.append(m.group(1))
    return scripts

# ============ M3U8 ÇÖZÜMLEME ============
def resolve_m3u8(channel):
    source_url = channel["source_url"]
    referer = channel["referer"]
    mode = channel["mode"]

    print(f"[*] {channel['name']} icin m3u8 cozumleniyor: {source_url}")

    html = fetch_url(source_url, referer=referer or source_url)
    if not html:
        print(f"[!] Kaynak sayfa alinamadi")
        return None

    # DEBUG: HTML'in ilk 2000 karakterini logla
    print(f"[DEBUG] HTML uzunluk: {len(html)}")
    print(f"[DEBUG] HTML ilk 500: {html[:500]!r}")

    # 1) Doğrudan m3u8 ara
    m3u8 = extract_m3u8_from_html(html)
    if m3u8:
        print(f"[+] m3u8 bulundu: {m3u8[:120]}")
        return m3u8

    # 2) iframe'leri bul (hepsini dene, sadece ad.html değil)
    iframes = re.findall(r'<iframe[^>]*src\s*=\s*["\']([^"\']+)["\']', html, re.IGNORECASE)
    print(f"[*] Bulunan iframe sayisi: {len(iframes)}")

    for iframe_src in iframes:
        if not iframe_src.startswith("http"):
            iframe_src = urllib.parse.urljoin(source_url, iframe_src)
        print(f"[*] iframe deneniyor: {iframe_src}")

        iframe_html = fetch_url(iframe_src, referer=source_url)
        if not iframe_html:
            continue

        m3u8 = extract_m3u8_from_html(iframe_html)
        if m3u8:
            print(f"[+] iframe icinde m3u8: {m3u8[:120]}")
            return m3u8

        # iframe içindeki iframe'leri de dene (nested)
        inner_iframes = re.findall(r'<iframe[^>]*src\s*=\s*["\']([^"\']+)["\']', iframe_html, re.IGNORECASE)
        for inner_src in inner_iframes:
            if not inner_src.startswith("http"):
                inner_src = urllib.parse.urljoin(iframe_src, inner_src)
            print(f"[*] nested iframe: {inner_src}")
            inner_html = fetch_url(inner_src, referer=iframe_src)
            if inner_html:
                m3u8 = extract_m3u8_from_html(inner_html)
                if m3u8:
                    print(f"[+] nested iframe icinde m3u8: {m3u8[:120]}")
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
        """Unicode-safe hata gönder"""
        try:
            self.send_response(code)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(message.encode("utf-8"))
        except Exception:
            pass

    def do_GET(self):
        try:
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
        except Exception:
            self._send_error_safe(400, "Bad request")
            return

        if path.startswith("/channel/"):
            slug = path[len("/channel/"):].strip("/")
            # URL decode et
            slug = urllib.parse.unquote(slug)

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

            # m3u8 içeriğini çek
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

        elif path == "/segment":
            query = urllib.parse.parse_qs(parsed.query)
            seg_url = query.get("url", [None])[0]
            ref = query.get("ref", [None])[0]
            if ref:
                ref = urllib.parse.unquote(ref)

            if not seg_url:
                self._send_error_safe(400, "url parametresi eksik")
                return

            seg_url = urllib.parse.unquote(seg_url)

            # Segment binary olabilir - byte olarak çek
            try:
                headers = {
                    "User-Agent": USER_AGENT,
                    "Accept": "*/*",
                    "Accept-Encoding": "identity",
                }
                if ref:
                    headers["Referer"] = ref
                    try:
                        p = urllib.parse.urlparse(ref)
                        headers["Origin"] = f"{p.scheme}://{p.netloc}"
                    except Exception:
                        pass

                req = urllib.request.Request(seg_url, headers=headers)
                with urllib.request.urlopen(req, timeout=20) as resp:
                    seg_data = resp.read()
                    content_type = resp.headers.get("Content-Type", "video/mp2t")
            except Exception as e:
                print(f"[!] segment hatasi: {e}")
                self._send_error_safe(502, "Segment alinamadi")
                return

            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(seg_data)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(seg_data)

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

        elif path == "/" or path == "/playlist.m3u":
            # Basit M3U playlist sun
            host = self.headers.get("Host", "localhost")
            scheme = "https" if self.headers.get("X-Forwarded-Proto") == "https" else "http"
            lines = ["#EXTM3U"]
            for slug, ch in CHANNELS.items():
                lines.append(f'#EXTINF:-1 tvg-id="{slug}" tvg-name="{ch["name"]}" tvg-logo="{ch["logo"]}" group-title="{ch["group"]}",{ch["name"]}')
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
            except Exception as e:
                print(f"[!] {channel['name']} hata: {e}")

# ============ MAIN ============
def main():
    print("=" * 60)
    print("  HLS Restream Proxy v2 - Railway")
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

    socketserver.ThreadingTCPServer.allow_reuse_address = True
    with socketserver.ThreadingTCPServer(("0.0.0.0", PORT), ProxyHandler) as httpd:
        httpd.serve_forever()

if __name__ == "__main__":
    main()
