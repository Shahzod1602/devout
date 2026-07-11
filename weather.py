"""Weather-on-reply: update xabaridagi "Current location:" bo'yicha ob-havo.

Oqim: guruhdagi update xabariga (updater yoki bot yuborgan, ichida
"Current location: <manzil>" qatori bor) kimdir "weather" deb reply qiladi →
manzil qatordan deterministik olinadi (LLM'siz) → Nominatim geocoding →
api.weather.gov (NWS) prognoz + faol ogohlantirishlar → guruhga INGLIZCHA javob.

Manba tanlovi: NWS — bepul, API-key'siz (yangi secret yo'q), cheksiz, rasmiy AQSh
manbasi va eng muhimi truck uchun ALERT'lar (High Wind / Winter Storm Warning).
Geocoding: Nominatim (update'lardagi manzillar OSM reverse-geocode formatida
keladi, shuning uchun search ham OSM'da eng aniq ishlaydi; 1 req/s policy —
TTL-cache bilan himoyalangan).

Barcha xatolar yutiladi — funksiya hech qachon raise qilmaydi, do'stona
inglizcha xabar qaytaradi (paperwork/ticket oqimlariga ta'sir zero).
"""
import logging
import re
import time

import httpx

logger = logging.getLogger(__name__)

# Nominatim va weather.gov ikkalasi ham identifikatsiya qiluvchi User-Agent talab qiladi.
_UA = {"User-Agent": "ClarvioBot/1.0 (logistics weather lookup)"}

_LOCATION_RE = re.compile(r"current\s*location\s*:\s*(.+)", re.IGNORECASE)

# TTL cache: bir xil manzil uchun qayta so'rov bo'lmasin (Nominatim policy + tezlik).
_CACHE_TTL = 600  # 10 daqiqa
_CACHE_MAX = 100
_cache: dict[str, tuple[float, str]] = {}

ERR_NO_LOCATION = (
    "Couldn't find a \"Current location:\" line in the replied message. "
    "Reply to a location update to get the weather."
)
ERR_UNAVAILABLE = "Weather service is unavailable right now. Please try again in a few minutes."


def extract_current_location(text: str) -> str | None:
    """Update matnidan "Current location: ..." qatorini oladi (qator oxirigacha)."""
    if not text:
        return None
    m = _LOCATION_RE.search(text)
    if not m:
        return None
    loc = m.group(1).splitlines()[0].strip()
    return loc or None


async def _geocode(location: str) -> tuple[float, float] | None:
    """Nominatim: erkin-format manzil → (lat, lon). Xatoda None."""
    try:
        async with httpx.AsyncClient(timeout=10, headers=_UA) as client:
            r = await client.get(
                "https://nominatim.openstreetmap.org/search",
                params={"q": location, "format": "json", "limit": 1},
            )
        if r.status_code != 200:
            return None
        items = r.json()
        if not items:
            return None
        return float(items[0]["lat"]), float(items[0]["lon"])
    except Exception:
        logger.debug("geocode xatosi: %s", location, exc_info=True)
        return None


async def _nws_weather(lat: float, lon: float) -> str | None:
    """NWS: joy nomi + 2 prognoz davri + faol alertlar → formatlangan matn. Xatoda None."""
    try:
        async with httpx.AsyncClient(timeout=15, headers=_UA, follow_redirects=True) as client:
            pr = await client.get(f"https://api.weather.gov/points/{lat:.4f},{lon:.4f}")
            if pr.status_code != 200:
                return None  # AQShdan tashqari nuqta ham shu yerga tushadi
            props = pr.json().get("properties") or {}
            forecast_url = props.get("forecast")
            rel = ((props.get("relativeLocation") or {}).get("properties")) or {}
            place = ", ".join(x for x in (rel.get("city"), rel.get("state")) if x) or f"{lat:.2f},{lon:.2f}"

            periods = []
            if forecast_url:
                fr = await client.get(forecast_url)
                if fr.status_code == 200:
                    periods = ((fr.json().get("properties") or {}).get("periods") or [])[:2]

            alerts = []
            ar = await client.get("https://api.weather.gov/alerts/active", params={"point": f"{lat:.4f},{lon:.4f}"})
            if ar.status_code == 200:
                for feat in (ar.json().get("features") or [])[:2]:
                    p = feat.get("properties") or {}
                    event = p.get("event")
                    if not event:
                        continue
                    ends = p.get("ends") or p.get("expires") or ""
                    alerts.append(f"⚠️ {event}" + (f" (until {ends[:16].replace('T', ' ')})" if ends else ""))

        if not periods and not alerts:
            return None

        lines = [f"🌤 Weather — {place}"]
        for p in periods:
            precip = (p.get("probabilityOfPrecipitation") or {}).get("value")
            precip_s = f", precip {precip}%" if isinstance(precip, int) and precip > 0 else ""
            lines.append(
                f"{p.get('name')}: {p.get('temperature')}°{p.get('temperatureUnit', 'F')}, "
                f"{p.get('shortForecast')}, wind {p.get('windDirection', '')} {p.get('windSpeed', '')}{precip_s}"
            )
        lines.extend(alerts)
        return "\n".join(lines)
    except Exception:
        logger.debug("NWS xatosi: %s,%s", lat, lon, exc_info=True)
        return None


async def get_weather_reply(replied_text: str) -> str:
    """Reply qilingan xabar matnidan to'liq javob xabarini quradi (hech qachon raise qilmaydi)."""
    location = extract_current_location(replied_text)
    if not location:
        return ERR_NO_LOCATION

    now = time.time()
    cached = _cache.get(location)
    if cached and now - cached[0] < _CACHE_TTL:
        return cached[1]

    coords = await _geocode(location)
    if not coords:
        return ERR_UNAVAILABLE
    report = await _nws_weather(*coords)
    if not report:
        return ERR_UNAVAILABLE

    if len(_cache) >= _CACHE_MAX:
        _cache.pop(min(_cache, key=lambda k: _cache[k][0]), None)
    _cache[location] = (now, report)
    return report
