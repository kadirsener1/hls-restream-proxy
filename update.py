import re
import requests

embed_url = "https://www.canlitv.you/embed/?id=12013"
headers = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML,"
        " like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.canlitv.you/",
}

try:
  res = requests.get(embed_url, headers=headers)
  html = res.text

  # m3u8 linkini regex ile yakala
  match = re.search(
      r"https?:\/\/[^\"'\s<>]+?\.m3u8[^\"'\s<>0-9a-zA-Z]*[^\s\"']*",
      html,
      re.IGNORECASE,
  )
  if match:
    m3u8_url = match.group(0).strip("'\"")
    if m3u8_url.startswith("https:/") and not m3u8_url.startswith("https://"):
      m3u8_url = m3u8_url.replace("https:/", "https://")

    print(f"Güncel m3u8 bulundu: {m3u8_url}")

    # TiviMate uyumlu m3u içeriği oluştur
    m3u_content = f"""#EXTM3U
#EXTINF:-1 tvg-id="" tvg-name="Canli TV" group-title="Canli TV", Canli TV
{m3u8_url}
"""
    with open("channel.m3u", "w", encoding="utf-8") as f:
      f.write(m3u_content)
    print("channel.m3u başarıyla güncellendi.")
  else:
    print("Hata: m3u8 linki sayfada bulunamadı.")
except Exception as e:
  print(f"Kritik Hata: {e}")
