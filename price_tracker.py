# price_tracker.py
"""
Enhanced price tracker:
- products from products.csv or built-in PRODUCTS
- --test to send a test embed
- --digest to send a single summary embed
- --convert to add SAR conversions (exchangerate.host)
- keeps price history in price_history.json for sparkline
"""

import os, sys, time, json, argparse, requests
from datetime import datetime

# -------------------------
# Config
# -------------------------
API_KEY = os.getenv("RAINFOREST_KEY")
WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK")

# fallback product list (used when products.csv missing)
PRODUCTS = [
    {"asin": "B0D9V6M5L8", "title": "Spider-Man: Blue"},
    {"asin": "B0D2LM8NJK", "title": "The Flash: Rebirth"},
    {"asin": "B0CYK8W4DJ", "title": "Superman: The Rebirth Omnibus by Peter J. Tomasi & Patrick Gleason"},
    {"asin": "B0CKVZCSX3", "title": "Tim Drake: Robin Vol. 1"},
    {"asin": "B07XL7C9C1", "title": "Invincible Compendium Vol. 1"},
    {"asin": "B07Y3Y1TQK", "title": "Harleen"},
    {"asin": "B0D2X9L9ND", "title": "Absolute Superman for All Seasons"},
    {"asin": "B0C1WBN7NY", "title": "Daredevil by Frank Miller & Klaus Janson Omnibus"},
    {"asin": "B0CXZJFR8V", "title": "Justice League: Origin (The New 52)"},
    {"asin": "B0C6S9ZVBY", "title": "Ultimate Spider-Man Omnibus Vol. 1"},
    {"asin": "B0CP9VFXRB", "title": "Ultimate Spider-Man (2024) Vol. 1"},
]

MARKETS = {
    "sa": {"domain": "amazon.sa", "label": "Amazon.sa", "currency": "SAR"},
    "us": {"domain": "amazon.com", "label": "Amazon.com", "currency": "USD"},
    "uk": {"domain": "amazon.co.uk", "label": "Amazon.co.uk", "currency": "GBP"},
}

DATA_PREV = "previous_prices.json"
DATA_HISTORY = "price_history.json"
HISTORY_KEEP = 10  # keep last N datapoints per market per ASIN

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; PriceTracker/1.0; +https://example.com)"}

# -------------------------
# Helpers
# -------------------------
def load_products():
    csv_path = "products.csv"
    if os.path.exists(csv_path):
        out = []
        with open(csv_path, "r", encoding="utf-8") as f:
            for i, line in enumerate(f):
                if not line.strip():
                    continue
                line = line.strip()
                # allow header
                if i == 0 and (line.lower().startswith("asin") or "," not in line):
                    # try to detect header: if contains 'asin' we skip
                    if "asin" in line.lower():
                        continue
                parts = [p.strip() for p in line.split(",", 1)]
                if len(parts) == 2:
                    out.append({"asin": parts[0], "title": parts[1]})
                else:
                    out.append({"asin": parts[0], "title": parts[0]})
        if out:
            print(f"Loaded {len(out)} products from products.csv")
            return out
    print("Using built-in PRODUCTS list")
    return PRODUCTS

def load_json(path):
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return {}

def save_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)

def fetch_rainforest(asin, domain):
    base = "https://api.rainforestapi.com/request"
    params = {"api_key": API_KEY, "type": "product", "amazon_domain": domain, "asin": asin}
    try:
        r = requests.get(base, params=params, timeout=30, headers=HEADERS)
        r.raise_for_status()
        return r.json().get("product", {})
    except Exception as e:
        print(f"fetch error {asin} @ {domain}: {e}")
        return {}

def pick_price_from_product(product):
    # prefer buybox_winner price, fallback to offers list
    price = None; currency = None
    bb = product.get("buybox_winner")
    if bb and bb.get("price"):
        price = bb["price"].get("value")
        currency = bb["price"].get("currency")
    if price is None:
        offers = product.get("offers") or []
        if offers:
            first = offers[0]
            p = first.get("price") or {}
            price = p.get("value")
            currency = p.get("currency")
    return price, currency

def format_price(p, cur):
    if p is None:
        return "N/A"
    try:
        if int(p) == p:
            return f"{int(p)} {cur}"
    except:
        pass
    return f"{p:.2f} {cur}"

def pct(old, new):
    try:
        if old is None or new is None:
            return None
        if old == 0:
            return None
        return (new - old) / old * 100.0
    except:
        return None

def sparkline(values):
    # values: list of numbers (may include None)
    vals = [v for v in values if v is not None]
    if not vals:
        return ""
    chars = "▁▂▃▄▅▆▇█"
    mn, mx = min(vals), max(vals)
    if mn == mx:
        return chars[0] * len(values)
    out = []
    for v in values:
        if v is None:
            out.append(" ")
        else:
            idx = int((v - mn) / (mx - mn) * (len(chars) - 1))
            out.append(chars[idx])
    return "".join(out)

def convert_rates():
    # convert USD->SAR and GBP->SAR once per run (free exchangerate.host)
    try:
        r = requests.get("https://api.exchangerate.host/latest", params={"base": "USD", "symbols":"SAR,GBP"}, timeout=10)
        data = r.json()
        usd_to_sar = None
        gbp_to_sar = None
        if data.get("rates"):
            usd_to_sar = data["rates"].get("SAR")
            # compute GBP->SAR via USD base if needed
            # We will query GBP->SAR directly to be safer:
        r2 = requests.get("https://api.exchangerate.host/latest", params={"base":"GBP", "symbols":"SAR"}, timeout=10)
        d2 = r2.json()
        gbp_to_sar = d2.get("rates", {}).get("SAR")
        return {"USD": usd_to_sar, "GBP": gbp_to_sar}
    except Exception as e:
        print("Currency conversion error:", e)
        return {"USD": None, "GBP": None}

def build_product_summary(asin, title, infos, prev_infos, convert=False, rates=None):
    # infos: market_code -> {price,currency,link,image,availability}
    lines = []
    biggest_drop = None
    cheapest = None
    for mc, meta in MARKETS.items():
        info = infos.get(mc, {})
        prev = prev_infos.get(mc, {}) if prev_infos else {}
        curp = info.get("price")
        curc = info.get("currency") or meta.get("currency")
        prevp = prev.get("price")
        change_pct = None
        if prevp is not None and curp is not None and prevp != curp:
            change_pct = pct(prevp, curp)
        line = f"**{meta['label']}**: {format_price(curp, curc)}"
        if prevp is not None:
            if curp != prevp:
                sign = "🔻" if curp < prevp else "🔺"
                pct_text = f"{abs(change_pct):.0f}%" if change_pct is not None else ""
                line += f"  {sign} ({format_price(prevp, curc)} → {format_price(curp, curc)}) {pct_text}"
            else:
                line += f" (no change)"
        if convert and curc in ("USD","GBP") and rates:
            rate = rates.get(curc)
            if rate:
                conv = curp * rate if curp is not None else None
                line += f" · ≈ {format_price(conv, 'SAR')}"
        lines.append(line)
        # cheapest calc naive (numeric)
        try:
            if curp is not None:
                if cheapest is None or curp < cheapest[0]:
                    cheapest = (curp, curc, mc)
        except:
            pass
        # biggest drop
        if change_pct is not None and curp < prevp:
            if biggest_drop is None or change_pct < biggest_drop[0]:
                biggest_drop = (change_pct, mc, prevp, curp)
    return lines, biggest_drop, cheapest

def send_discord_embed(embed):
    payload = {"embeds": [embed]}
    try:
        r = requests.post(WEBHOOK_URL, json=payload, timeout=20)
        if r.status_code in (200,204):
            return True
        else:
            print("Discord HTTP", r.status_code, r.text)
            return False
    except Exception as e:
        print("Discord send error:", e)
        return False

# -------------------------
# Main
# -------------------------
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true", help="Send a test embed and exit")
    parser.add_argument("--digest", action="store_true", help="Send a single digest summary embed (instead of per-product)")
    parser.add_argument("--convert", action="store_true", help="Convert USD/GBP to SAR and include conversions")
    args = parser.parse_args()

    if API_KEY is None or WEBHOOK_URL is None:
        print("Missing RAINFOREST_KEY or DISCORD_WEBHOOK environment variables.")
        sys.exit(1)

    products = load_products()
    prev_all = load_json(DATA_PREV)
    history = load_json(DATA_HISTORY)

    if args.test:
        # quick test embed
        embed = {
            "title": "✅ Price Tracker — Test",
            "description": "This is a test embed. The tracker is configured correctly.",
            "color": 0x2ECC71,
            "timestamp": datetime.utcnow().isoformat()
        }
        send_discord_embed(embed)
        print("Test embed sent.")
        return

    rates = None
    if args.convert:
        rates = convert_rates()
        print("Rates:", rates)

    updates = []  # collect per-product updates for digest
    any_sent = False

    for p in products:
        asin = p["asin"]
        title_hint = p.get("title")
        print(f"Checking {title_hint} ({asin}) ...")
        infos = {}
        # fetch three marketplaces
        for mc, meta in MARKETS.items():
            prod = fetch_rainforest(asin, meta["domain"])
            price, currency = pick_price_from_product(prod)
            infos[mc] = {
                "price": price,
                "currency": currency or meta["currency"],
                "link": prod.get("link") or meta["label"] + " link",
                "image": prod.get("image") or prod.get("main_image"),
                "availability": prod.get("availability") or prod.get("availability_type"),
                "title": prod.get("title") or title_hint
            }
            time.sleep(1.0)  # throttle

        prev_infos = prev_all.get(asin, {})
        # detect change: compare numeric price per market
        changed = False
        for mc in MARKETS.keys():
            prevp = prev_infos.get(mc, {}).get("price")
            curp = infos.get(mc, {}).get("price")
            if (prevp is None and curp is not None) or (prevp is not None and curp is not None and prevp != curp):
                changed = True
                break

        # update history
        hist_entry = history.setdefault(asin, {})
        for mc in MARKETS.keys():
            hist_entry.setdefault(mc, [])
            curp = infos[mc].get("price")
            ts = datetime.utcnow().isoformat()
            if curp is not None:
                hist_entry[mc].append({"t": ts, "p": curp})
                # trim
                if len(hist_entry[mc]) > HISTORY_KEEP:
                    hist_entry[mc] = hist_entry[mc][-HISTORY_KEEP:]

        # save current snapshot to prev_all immediately (so next run sees it)
        # store only price + currency
        prev_all[asin] = {mc: {"price": infos[mc].get("price"), "currency": infos[mc].get("currency")} for mc in MARKETS.keys()}

        # build update summary
        lines, biggest_drop, cheapest = build_product_summary(asin, infos["sa"].get("title") or title_hint, infos, prev_infos, convert=args.convert, rates=rates)
        spark_lines = {}
        for mc in MARKETS.keys():
            vals = [d.get("p") for d in history.get(asin, {}).get(mc, [])]
            spark_lines[mc] = sparkline(vals)

        if changed:
            updates.append({
                "asin": asin,
                "title": infos["sa"].get("title") or title_hint,
                "infos": infos,
                "lines": lines,
                "biggest_drop": biggest_drop,
                "cheapest": cheapest,
                "sparks": spark_lines
            })
        else:
            print("No price changes for this product.")

    # Save history & prev snapshot
    save_json(DATA_HISTORY, history)
    save_json(DATA_PREV, prev_all)

    if not updates:
        print("No updates to send.")
        return

    # Send digest or per-product embeds
    if args.digest:
        # Single embed with multiple fields (limit size concerns for many products)
        description = f"Price updates — {len(updates)} items\n"
        fields = []
        for u in updates:
            name = f"{u['title']} ({u['asin']})"
            # compose small multiline value
            value = ""
            for l in u["lines"]:
                value += l + "\n"
            # append sparkline for SA only (compact)
            sp = u["sparks"].get("sa") or ""
            if sp:
                value += f"\n`{sp}`"
            link_sa = u["infos"]["sa"].get("link")
            if link_sa:
                value += f"\n[View on Amazon.sa]({link_sa})"
            fields.append({"name": name[:256], "value": value[:1024], "inline": False})
        embed = {
            "title": "📦 Price Digest",
            "description": description,
            "timestamp": datetime.utcnow().isoformat(),
            "fields": fields,
            "color": 0xF1C40F
        }
        ok = send_discord_embed(embed)
        if ok:
            print("Sent digest embed.")
            any_sent = True
    else:
        # per-product embeds
        for u in updates:
            # build rich embed
            title = u["title"]
            desc = ""
            if u["biggest_drop"]:
                pct_text = f"{abs(u['biggest_drop'][0]):.0f}% off"
                desc = f"🔥 **{pct_text}**\n"
            if u["cheapest"]:
                cheapest_text = f"Cheapest: **{MARKETS[u['cheapest'][2]]['label']}** — {format_price(u['cheapest'][0], u['cheapest'][1])}\n"
                desc += cheapest_text
            fields = []
            for l in u["lines"]:
                fields.append({"name": "\u200b", "value": l, "inline": False})
            # add sparkline
            sp = u["sparks"].get("sa")
            if sp:
                fields.append({"name": "Price history (SA)", "value": f"`{sp}`", "inline": False})
            embed = {
                "title": title,
                "url": u["infos"]["sa"].get("link"),
                "description": desc.strip(),
                "timestamp": datetime.utcnow().isoformat(),
                "fields": fields,
                "color": 0x2ECC71 if u["biggest_drop"] else 0x95A5A6
            }
            # thumbnail
            img = u["infos"]["sa"].get("image") or u["infos"]["us"].get("image") or u["infos"]["uk"].get("image")
            if img:
                embed["thumbnail"] = {"url": img}
            ok = send_discord_embed(embed)
            if ok:
                any_sent = True
                time.sleep(1.0)

    if any_sent:
        print("Updates sent.")
    else:
        print("No updates could be sent (Discord failures).")

if __name__ == "__main__":
    main()
