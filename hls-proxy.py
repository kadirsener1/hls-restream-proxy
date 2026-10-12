#!/usr/bin/env python3
"""
HLS Restream Proxy
Sabit /channel/<slug> URL'leri sunar, arka planda m3u8 linkini ve token'ı yeniler.
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
from datetime import datetime, timedelta

# ============ AYARLAR ============
PORT = int(os.environ.get("PORT", 8089))
CACHE_TTL = int(os.environ.get("CACHE_TTL", 3600))  # 1 saat cache
REFRESH_INTERVAL = int(os.environ.get("REFRESH_INTERVAL", 14400))  # 4 saat

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64; rv:149.0) Gecko/20100101 Firefox/149.0"

# ============ KANAL YAPILANDIRMASI ============
# format: slug|Display Name|chno|logo_url|Group|source_page_url|mode|referer
CHANNELS = {}

def load_channels():
    """channels.conf dosyasını yükle"""
    global CHANNELS
    config_path = os.environ.get("CHANNELS_CONF", "channels.conf")
    
    if not os.path.exists(config_path):
        print(f"[!] {config_path} bulunamadı!")
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
                "mode": parts[6].strip() if len(parts) > 6 else "iframe",
                "referer": parts[7].strip() if len(parts) > 7 else "",
                "m3u8_url": None,
                "last_refresh": None,
            }
    
    print(f"[+] {len(CHANNELS)} kanal yüklendi.")

# ============ M3U8 ÇIKARMA ============

def fetch_url(url, referer=None, timeout=15):
    """Basit HTTP GET"""
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.5",
    }
    if referer:
        headers["Referer"] = referer
    
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="ignore")
    except Exception as e:
        print(f"[!] fetch_url hatası ({url[:80]}): {e}")
        return None

def extract_m3u8_from_html(html):
    """HTML içinden m3u8 URL'sini çıkar"""
    if not html:
        return None
    
    # HTML comment'leri temizle
    html = re.sub(r'<!--.*?-->', '', html, flags=re.DOTALL)
    
    patterns = [
        r'file\s*:\s*["\']([^"\']*\.m3u8[^"\']*)["\']',
        r'src\s*=\s*["\']([^"\']*\.m3u8[^"\']*)["\']',
        r'data-src\s*=\s*["\']([^"\']*\.m3u8[^"\']*)["\']',
        r'["\']url["\']\s*:\s*["\']([^"\']*\.m3u8[^"\']*)["\']',
        r'(https?://[^\s"\'<>\\]+\.m3u8[^\s"\'<>\\]*)',
    ]
    
    for pattern in patterns:
        match = re.search(pattern, html, re.IGNORECASE)
        if match:
            url = match.group(1).strip()
            if len(url) > 20:
                return url
    
    return None

def extract_iframe_url(html, base_url):
    """HTML'den iframe URL'sini çıkar"""
    if not html:
        return None
    
    match = re.search(r'<iframe[^>]*src\s*=\s*["\']([^"\']+)["\']', html, re.IGNORECASE)
    if match:
        iframe_src = match.group(1).strip()
        if not iframe_src.startswith("http"):
            iframe_src = urllib.parse.urljoin(base_url, iframe_src)
        return iframe_src
    
    return None

def resolve_m3u8(channel):
    """Bir kanal için güncel m3u8 URL'sini al"""
    source_url = channel["source_url"]
    referer = channel["referer"]
    mode = channel["mode"]
    
    print(f"[*] {channel['name']} için m3u8 çözümleniyor...")
    
    html = fetch_url(source_url, referer=referer)
    if not html:
        print(f"[!] Kaynak sayfa alınamadı: {source_url}")
        return None
    
    # Doğrudan m3u8 var mı?
    m3u8 = extract_m3u8_from_html(html)
    if m3u8:
        print(f"[+] Doğrudan m3u8 bulundu: {m3u8[:100]}...")
        return m3u8
    
    # iframe içinde mi?
    if mode == "iframe":
        iframe_url = extract_iframe_url(html, source_url)
        if iframe_url:
            print(f"[*] iframe bulundu: {iframe_url[:80]}...")
            iframe_html = fetch_url(iframe_url, referer=referer)
            if iframe_html:
                m3u8 = extract_m3u8_from_html(iframe_html)
                if m3u8:
                    print(f"[+] iframe içinde m3u8 bulundu: {m3u8[:100]}...")
                    return m3u8
    
    print(f"[!] m3u8 bulunamadı: {channel['name']}")
    return None

# ============ PROXY HANDLER ============

class ProxyHandler(http.server.BaseHTTPRequestHandler):
    
    def log_message(self, format, *args):
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {args[0]}")
    
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        
        # /channel/<slug> isteği
        if path.startswith("/channel/"):
            slug = path[len("/channel/"):].strip("/")
            
            if slug not in CHANNELS:
                self.send_error(404, f"Kanal bulunamadı: {slug}")
                return
            
            channel = CHANNELS[slug]
            
            # Cache kontrolü
            now = datetime.now()
            if (channel["m3u8_url"] and channel["last_refresh"] and 
                (now - channel["last_refresh"]).total_seconds() < CACHE_TTL):
                m3u8_url = channel["m3u8_url"]
                print(f"[+] Cache'den sunuluyor: {slug}")
            else:
                m3u8_url = resolve_m3u8(channel)
                if m3u8_url:
                    channel["m3u8_url"] = m3u8_url
                    channel["last_refresh"] = now
                else:
                    # Eski URL varsa onu kullan (fallback)
                    m3u8_url = channel["m3u8_url"]
            
            if not m3u8_url:
                self.send_error(503, "Yayın şu anda alınamıyor")
                return
            
            # M3U8 içeriğini al ve proxy'le
            m3u8_content = fetch_url(m3u8_url, referer=channel.get("referer"))
            if not m3u8_content:
                self.send_error(502, "Upstream m3u8 alınamadı")
                return
            
            # Playlist içindeki segment URL'lerini proxy üzerinden yönlendir
            base_url = m3u8_url.rsplit("/", 1)[0]
            lines = m3u8_content.split("\n")
            rewritten = []
            
            for line in lines:
                line = line.strip()
                if line and not line.startswith("#"):
                    # Segment veya alt playlist URL'si
                    if not line.startswith("http"):
                        line = f"{base_url}/{line}"
                    # Kendi proxy'miz üzerinden geçir
                    proxy_seg = f"/segment?url={urllib.parse.quote(line)}&ref={urllib.parse.quote(channel.get('referer', ''))}"
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
        
        # /segment?url=...&ref=... isteği
        elif path == "/segment":
            query = urllib.parse.parse_qs(parsed.query)
            seg_url = query.get("url", [None])[0]
            ref = query.get("ref", [None])[0]
            
            if not seg_url:
                self.send_error(400, "url parametresi eksik")
                return
            
            seg_content = fetch_url(seg_url, referer=ref)
            if not seg_content:
                self.send_error(502, "Segment alınamadı")
                return
            
            self.send_response(200)
            self.send_header("Content-Type", "video/mp2t")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(seg_content.encode("utf-8", errors="ignore"))
        
        # /health kontrolü
        elif path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "status": "ok",
                "channels": len(CHANNELS),
                "time": datetime.now().isoformat()
            }).encode())
        
        else:
            self.send_error(404, "Not found")

# ============ ARKA PLAN YENİLEYİCİ ============

def background_refresher():
    """Arka planda periyodik olarak tüm kanalların m3u8 linklerini yenile"""
    while True:
        time.sleep(REFRESH_INTERVAL)
        print(f"\n[🔄] Arka plan yenileme başlıyor: {datetime.now()}")
        
        for slug, channel in CHANNELS.items():
            try:
                m3u8_url = resolve_m3u8(channel)
                if m3u8_url:
                    channel["m3u8_url"] = m3u8_url
                    channel["last_refresh"] = datetime.now()
                    print(f"[✓] {channel['name']} yenilendi.")
                else:
                    print(f"[!] {channel['name']} yenilenemedi.")
            except Exception as e:
                print(f"[!] {channel['name']} yenileme hatası: {e}")
        
        print(f"[🔄] Arka plan yenileme tamamlandı.\n")

# ============ ANA ÇALIŞTIRICI ============

def main():
    print("=" * 60)
    print("  HLS Restream Proxy - Railway Edition")
    print("=" * 60)
    
    # Kanalları yükle
    load_channels()
    
    if not CHANNELS:
        print("[!] Hiç kanal yüklenemedi! channels.conf dosyasını kontrol et.")
        return
    
    # Tüm kanalları başlangıçta bir kez çöz
    print("\n[*] Başlangıç m3u8 çözümlemesi yapılıyor...")
    for slug, channel in CHANNELS.items():
        m3u8_url = resolve_m3u8(channel)
        if m3u8_url:
            channel["m3u8_url"] = m3u8_url
            channel["last_refresh"] = datetime.now()
    
    # Arka plan yenileyiciyi başlat
    refresher = threading.Thread(target=background_refresher, daemon=True)
    refresher.start()
    print(f"[+] Arka plan yenileyici başlatıldı ({REFRESH_INTERVAL}s aralıkla).")
    
    # HTTP sunucusunu başlat
    print(f"[+] HTTP sunucusu başlatılıyor: port {PORT}")
    print(f"[+] Kullanılabilir kanallar:")
    for slug, ch in CHANNELS.items():
        status = "✓" if ch["m3u8_url"] else "✗"
        print(f"    {status} /channel/{slug} - {ch['name']}")
    print("=" * 60)
    
    with socketserver.ThreadingTCPServer(("0.0.0.0", PORT), ProxyHandler) as httpd:
        httpd.serve_forever()

if __name__ == "__main__":
    main()
