import os
import re
import random
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from flask import Flask, jsonify, request
from flask_cors import CORS
from ddgs import DDGS

app = Flask(__name__)
CORS(app)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
}
TIMEOUT = 8
MAX_COUNT = 100

# Terms that indicate the query is already car-related
CAR_TERMS = {
    "car", "cars", "vehicle", "vehicles", "auto", "automobile", "automobiles",
    "truck", "suv", "sedan", "coupe", "hatchback", "convertible", "van",
    "pickup", "crossover", "wagon", "supercar", "hypercar",
    "toyota", "honda", "ford", "bmw", "mercedes", "audi", "volkswagen",
    "chevrolet", "chevy", "nissan", "hyundai", "kia", "mazda", "subaru",
    "lexus", "infiniti", "acura", "cadillac", "buick", "gmc", "dodge",
    "chrysler", "jeep", "ram", "tesla", "porsche", "ferrari", "lamborghini",
    "maserati", "bentley", "rolls", "bugatti", "mclaren", "aston",
    "jaguar", "rover", "volvo", "peugeot", "renault", "fiat", "alfa",
    "mitsubishi", "suzuki", "isuzu", "genesis", "rivian", "lucid",
    "polestar", "mini", "skoda", "camry", "civic", "mustang", "corvette",
    "charger", "challenger", "wrangler", "4runner", "highlander", "rav4",
    "cr-v", "f-150", "silverado", "tundra", "tacoma",
}

# Image file extensions to validate URLs
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp")


def enforce_car_query(query: str) -> str:
    """Prepend 'car' if the query contains no car-related term."""
    words = set(query.lower().split())
    if not words.intersection(CAR_TERMS):
        return f"car {query}"
    return query


def fetch_duckduckgo(query: str, limit: int) -> list[str]:
    """Fetch car image URLs from DuckDuckGo."""
    try:
        results = DDGS().images(query, max_results=limit)
        urls = [r["image"] for r in results if r.get("image")]
        print(f"[duckduckgo] '{query}' -> {len(urls)} images")
        return urls
    except Exception as e:
        print(f"[duckduckgo] failed for '{query}': {e}")
        return []


def fetch_bing(query: str, limit: int) -> list[str]:
    """Fetch car image URLs from Bing Images async endpoint."""
    try:
        encoded = requests.utils.quote(query)
        # Use Bing's async endpoint — returns consistent JSON blobs
        url = (
            f"https://www.bing.com/images/async"
            f"?q={encoded}&first=1&count={min(limit, 50)}"
            f"&adlt=off&qft=+filterui:photo-photo"
        )
        res = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        res.raise_for_status()

        # Primary: murl field in Bing's JSON data blobs
        matches = re.findall(r'"murl"\s*:\s*"(https?://[^"]+)"', res.text)

        # Fallback: src attributes in <img> tags
        if not matches:
            matches = re.findall(r'<img[^>]+src="(https?://[^"]+)"', res.text)

        if not matches:
            print(
                f"[bing] no matches — status {res.status_code}, "
                f"snippet: {res.text[:400]!r}"
            )
            return []

        # Decode unicode escapes, keep only real image URLs
        urls = []
        for m in matches:
            try:
                decoded = m.encode().decode("unicode_escape")
            except Exception:
                decoded = m
            # Skip Bing's own CDN thumbnails (th.bing.com) — keep original sources
            if decoded.startswith("http") and "th.bing.com" not in decoded:
                urls.append(decoded)

        urls = urls[:limit]
        print(f"[bing] '{query}' -> {len(urls)} images")
        return urls
    except Exception as e:
        print(f"[bing] failed for '{query}': {e}")
        return []


@app.route("/api/search")
def search():
    raw_query = request.args.get("q", "").strip()
    if not raw_query:
        return jsonify({"status": "error", "message": "Missing q parameter"}), 400

    # Always enforce car-only results
    query = enforce_car_query(raw_query)

    try:
        count = int(request.args.get("count", 70))
    except (ValueError, TypeError):
        count = 70
    count = min(count, MAX_COUNT)

    per_engine = count // 2 if count >= 2 else 1

    ddg_results: list[str] = []
    bing_results: list[str] = []
    engines_used: list[str] = []

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = {
            executor.submit(fetch_duckduckgo, query, per_engine): "duckduckgo",
            executor.submit(fetch_bing, query, per_engine): "bing",
        }
        for future in as_completed(futures):
            engine = futures[future]
            try:
                result = future.result()
            except Exception as e:
                print(f"[{engine}] future error: {e}")
                result = []
            if engine == "duckduckgo":
                ddg_results = result
            else:
                bing_results = result

    # Shortfall fill — one extra call to the working engine, no loops
    if not ddg_results and bing_results:
        shortfall = count - len(bing_results)
        if shortfall > 0:
            extra = fetch_bing(query, shortfall)
            bing_results = (bing_results + extra)[:count]
    elif not bing_results and ddg_results:
        shortfall = count - len(ddg_results)
        if shortfall > 0:
            extra = fetch_duckduckgo(query, shortfall)
            ddg_results = (ddg_results + extra)[:count]

    if ddg_results:
        engines_used.append("duckduckgo")
    if bing_results:
        engines_used.append("bing")

    # Deduplicate while preserving order, then shuffle for a good mix
    seen: set[str] = set()
    combined: list[str] = []
    for url in ddg_results + bing_results:
        if url not in seen:
            seen.add(url)
            combined.append(url)

    random.shuffle(combined)
    combined = combined[:count]

    return jsonify({
        "status": "success",
        "query_used": query,
        "engines_used": engines_used,
        "count": len(combined),
        "images": combined,
    })


@app.route("/api/ping")
def ping():
    return jsonify({"status": "alive"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
