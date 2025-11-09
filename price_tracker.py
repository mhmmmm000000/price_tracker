import requests
import json
import os
import time
from datetime import datetime

API_KEY = os.getenv("RAINFOREST_KEY")
WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK")

# ASINs to track (you can replace/add)
PRODUCTS = {
    "B0D36G1MZ4": "Batman: Hush",
    "B0D9V6M5L8": "Spider-Man: Blue",
    "B0D2LM8NJK": "The Flash: Rebirth"
}

DATA_FILE = "previous_prices.json"

MARKETS = {
    "sa": {"amazon_domain": "amazon.sa", "label": "Amazon.sa", "url": "https://www.amazon.sa/dp/{}"},
    "us": {"amazon_domain": "amazon.com", "label": "Amazon.com", "url": "https://www.amazon.com/dp/{}"},
    "uk": {"amazon_domain": "amazon.co.uk", "label": "Amazon.co.uk", "url": "https://www.amazon.co.uk/dp/{}"},
}

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; PriceTracker/1.0; +https://example.com)"}


def load_previous():
    if not os.path.exists(DATA_FILE):
        return {}
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except:
            return {}


def save_current(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def fetch_market_info(asin, amazon_domain):
    """
    Returns a dict like:
    { "price": float or None, "currency": str or None, "title": str or None,
      "link": str or None, "image": str or None, "availability": str or None,
      "rating": float or None, "reviews": int or None }
    """
    base_url = "https://api.rainforestapi.com/request"
    params = {
        "api_key": API_KEY,
        "type": "product",
        "amazon_domain": amazon_domain,
        "asin": asin
    }
    try:
        r = requests.get(base_url, params=params, timeout=30, headers=HEADERS)
        r.raise_for_status()
        data = r.json()
        product = data.get("product", {})

        # price
        price = None
        currency = None
        buybox = product.get("buybox_winner") or {}
        if buybox:
            p = buybox.get("price", {})
            price = p.get("value")
            currency = p.get("currency")

        # fallback: offers list
        if price is None:
            offers = product.get("offers", [])
            if offers:
                first = offers[0]
                p = first.get("price", {})
                price = p.get("value")
                currency = p.get("currency")

        title = product.get("title")
        link = product.get("link") or (f"https://{amazon_domain}/dp/{asin}")
        image = None
        # Rainforest sometimes returns "main_image" or "images" or "image"
        image = product.get("image") or product.get("main_image") or (
            (product.get("images") or [None])[0]
        )

        availability = product.get("availability") or product.get("availability_type")
        rating = None
        reviews = None
        if product.get("rating"):
            try:
                rating = float(product.get("rating"))
            except:
                rating = None
        if product.get("reviews"):
            try:
                reviews = int(product.get("reviews"))
            except:
                reviews = None

        return {
            "price": price,
            "currency": currency,
            "title": title,
            "link": link,
            "image": image,
            "availability": availability,
            "rating": rating,
            "reviews": reviews
        }

    except Exception as e:
        print(f"Error fetching {asin} on {amazon_domain}: {e}")
        return {
            "price": None,
            "currency": None,
            "title": None,
            "link": f"https://{amazon_domain}/dp/{asin}",
            "image": None,
            "availability": None,
            "rating": None,
            "reviews": None
        }


def format_price(p, cur):
    if p is None:
        return "N/A"
    # keep two decimals when not integer
    if int(p) == p:
        return f"{int(p)} {cur}"
    else:
        return f"{p:.2f} {cur}"


def pct_change(old, new):
    try:
        if old is None or new is None:
            return None
        return ((new - old) / old) * 100 if old != 0 else None
    except:
        return None


def build_embed(asin, title, market_infos, prev_infos):
    """
    Build a Discord embed dict. market_infos: dict of market_code -> info dict
    prev_infos: previous market price info dict (from file)
    """
    # decide overall status (new / drop / rise / same)
    status = "No change"
    any_drop = False
    any_change = False
    drops = []
    fields = []

    # thumbnail: try to get any image from markets (priority: sa, us, uk)
    image_url = None
    for m in ("sa", "us", "uk"):
        if market_infos.get(m) and market_infos[m].get("image"):
            image_url = market_infos[m]["image"]
            break

    # compare markets and create fields
    cheapest = None  # (price_value, currency, market_code)
    for mcode, meta in MARKETS.items():
        info = market_infos.get(mcode) or {}
        prev_market = prev_infos.get(mcode, {}) if prev_infos else {}
        cur_price = info.get("price")
        cur_cur = info.get("currency") or ""
        prev_price = prev_market.get("price")
        # choose display strings
        cur_str = format_price(cur_price, cur_cur) if cur_price is not None else "N/A"
        prev_str = format_price(prev_price, prev_market.get("currency") or cur_cur) if prev_price is not None else None

        # detect change
        changed = False
        change_pct = None
        if prev_price is None and cur_price is not None:
            changed = True
            any_change = True
        elif prev_price is not None and cur_price is not None and prev_price != cur_price:
            changed = True
            any_change = True
            change_pct = pct_change(prev_price, cur_price)
            if cur_price < prev_price:
                any_drop = True
                drops.append((mcode, prev_price, cur_price, change_pct))

        # cheapest calculation (only if numeric and same currency ignored)
        try:
            if cur_price is not None:
                # track cheapest by absolute numeric value (no currency conversion)
                if cheapest is None or (cur_price < cheapest[0]):
                    cheapest = (cur_price, cur_cur, mcode)
        except:
            pass

        field_name = f"{meta['label']}"
        if info.get("availability"):
            field_name += f" • {info.get('availability')}"

        field_value = cur_str
        if prev_str:
            if changed and cur_price is not None and prev_price is not None and cur_price < prev_price:
                # show previous as strikethrough then new
                field_value = f"~~{prev_str}~~ → **{cur_str}**"
            elif changed and prev_price is not None:
                # show previous then new (increase)
                field_value = f"~~{prev_str}~~ → **{cur_str}**"
            else:
                field_value = f"{cur_str} (was {prev_str})"

        # add small rating line
        rating_part = ""
        if info.get("rating"):
            r = info.get("rating")
            rv = info.get("reviews")
            rating_part = f"\n⭐ {r}" + (f" ({rv} reviews)" if rv else "")

        # link
        link = info.get("link") or meta["url"].format(asin)
        # add field
        fields.append({
            "name": field_name,
            "value": f"{field_value}\n🔗 [View]({link}){rating_part}",
            "inline": False
        })

    # build description and header (discount if any)
    description = ""
    if any_drop and drops:
        # pick the largest percent drop to highlight
        biggest = min(drops, key=lambda d: d[2])  # not perfect; we'll compute percent
        # compute best percent drop
        best_pct = None
        best_market = None
        for (m, oldp, newp, pct) in drops:
            if pct is None:
                continue
            if best_pct is None or pct < best_pct:
                best_pct = pct
                best_market = m
        if best_pct is not None:
            pct_text = f"{abs(best_pct):.0f}% off"
            description = f"🔥 **{pct_text}**\n\n"
    # cheapest badge
    cheapest_text = ""
    if cheapest:
        cheapest_text = f"Cheapest: **{MARKETS[cheapest[2]]['label']}** — {format_price(cheapest[0], cheapest[1])}\n\n"

    # footer / affiliate note
    footer_text = "Price and availability subject to change. As an Amazon affiliate, I earn from qualifying purchases."

    # assemble embed
    embed = {
        "title": title or PRODUCTS.get(asin, asin),
        "url": market_infos.get("sa", {}).get("link") or MARKETS["sa"]["url"].format(asin),
        "description": (description + cheapest_text).strip(),
        "timestamp": datetime.utcnow().isoformat(),
        "fields": fields,
        "footer": {"text": footer_text},
    }
    if image_url:
        embed["thumbnail"] = {"url": image_url}

    # pick color: green if any drop, yellow if new, gray otherwise
    if any_drop:
        embed["color"] = 0x2ECC71  # green
    elif not any_change:
        embed["color"] = 0x95A5A6  # gray
    else:
        embed["color"] = 0xF1C40F  # yellow

    return embed


def send_embed_to_discord(embed):
    payload = {"embeds": [embed]}
    try:
        r = requests.post(WEBHOOK_URL, json=payload, timeout=20)
        if r.status_code not in (200, 204):
            print("Discord webhook error:", r.status_code, r.text)
        else:
            print("Sent embed to Discord.")
    except Exception as e:
        print("Error sending to Discord:", e)


def main():
    if API_KEY is None or WEBHOOK_URL is None:
        print("Missing RAINFOREST_KEY or DISCORD_WEBHOOK environment variables.")
        return

    prev_all = load_previous()
    current_all = {}
    any_updates = False

    for asin, label in PRODUCTS.items():
        print(f"\nChecking {label} ({asin}) ...")
        market_infos = {}
        prev_infos = prev_all.get(asin, {})

        # fetch markets
        for mcode, meta in MARKETS.items():
            info = fetch_market_info(asin, meta["amazon_domain"])
            market_infos[mcode] = info
            # small delay between requests
            time.sleep(1.0)

        # build a representation of current prices to save
        save_obj = {}
        for mcode in MARKETS.keys():
            info = market_infos.get(mcode, {})
            save_obj[mcode] = {
                "price": info.get("price"),
                "currency": info.get("currency")
            }

        current_all[asin] = save_obj

        # decide if there's a change (compare numeric price per market)
        changed_markets = []
        for mcode in MARKETS.keys():
            prev_price = None
            if prev_infos:
                prev_price = prev_infos.get(mcode, {}).get("price")
            cur_price = save_obj.get(mcode, {}).get("price")
            if (prev_price is None and cur_price is not None) or (prev_price is not None and cur_price is not None and prev_price != cur_price):
                changed_markets.append(mcode)

        if changed_markets:
            any_updates = True
            embed = build_embed(asin, market_infos.get("sa", {}).get("title") or label, market_infos, prev_infos)
            send_embed_to_discord(embed)
        else:
            print("No price changes for this product.")

    # save for next run
    save_current(current_all)

    if any_updates:
        print("\nUpdates were sent.")
    else:
        print("\nNo updates sent.")


if __name__ == "__main__":
    main()
