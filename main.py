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
    )
}
TIMEOUT = 8
MAX_COUNT = 100


def fetch_duckduckgo(query: str, limit: int) -> list[str]:
    """Fetch image URLs from DuckDuckGo using the ddgs library."""
    try:
        results = DDGS().images(query, max_results=limit)
        urls = [r["image"] for r in results if r.get("image")]
        print(f"[duckduckgo] '{query}' -> {len(urls)} images")
        return urls
    except Exception as e:
        print(f"[duckduckgo] failed for '{query}': {e}")
        return []


def fetch_bing(query: str, limit: int) -> list[str]:
    """Fetch image URLs from Bing image search."""
    try:
        url = f"https://www.bing.com/images/search?q={requests.utils.quote(query)}&form=HDRSC2&first=1"
        res = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        res.raise_for_status()
        matches = re.findall(r'"murl":"(https?[^"]+)"', res.text)
        if not matches:
            print(f"[bing] no matches — status {res.status_code}, response snippet: {res.text[:300]!r}")
        urls = []
        for m in matches:
            try:
                urls.append(m.encode().decode("unicode_escape"))
            except Exception:
                urls.append(m)
        urls = urls[:limit]
        print(f"[bing] '{query}' -> {len(urls)} images")
        return urls
    except Exception as e:
        print(f"[bing] failed for '{query}': {e}")
        return []


@app.route("/api/search")
def search():
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"status": "error", "message": "Missing q parameter"}), 400

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

    # Deduplicate while preserving order, then shuffle
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
        "engines_used": engines_used,
        "count": len(combined),
        "images": combined,
    })


@app.route("/api/ping")
def ping():
    return jsonify({"status": "alive"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
