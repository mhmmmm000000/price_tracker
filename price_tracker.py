import requests, json, os
from datetime import datetime

API_KEY = os.getenv("RAINFOREST_KEY")
WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK")
AMAZON_DOMAIN = "amazon.sa"

PRODUCTS = {
    "B0D36G1MZ4": "Batman: Hush",
    "B0D9V6M5L8": "Spider-Man: Blue",
    "B0D2LM8NJK": "The Flash: Rebirth"
}

DATA_FILE = "previous_prices.json"

def load_previous():
    if not os.path.exists(DATA_FILE):
        return {}
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_current(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def fetch_price(asin):
    url = "https://api.rainforestapi.com/request"
    params = {
        "api_key": API_KEY,
        "type": "product",
        "amazon_domain": AMAZON_DOMAIN,
        "asin": asin
    }
    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    data = r.json()
    price = data["product"]["buybox_winner"]["price"]["value"]
    currency = data["product"]["buybox_winner"]["price"]["currency"]
    return price, currency, data["product"]["title"]

def send_to_discord(msg):
    payload = {"content": msg}
    requests.post(WEBHOOK_URL, json=payload, timeout=20)

def main():
    print("Checking Amazon.sa prices...")
    prev = load_previous()
    current = {}
    changes = []

    for asin, name in PRODUCTS.items():
        try:
            price, currency, title = fetch_price(asin)
            current[asin] = {"price": price, "currency": currency}
            old = prev.get(asin, {}).get("price")
            if old is None or old != price:
                changes.append((title, price, currency, asin, old))
            print(f"{title}: {price} {currency}")
        except Exception as e:
            print(f"Error {asin}: {e}")

    if changes:
        msg = "**🟢 Price Updates Detected!**\n"
        for title, price, cur, asin, old in changes:
            url = f"https://www.amazon.sa/dp/{asin}"
            if old is None:
                msg += f"🆕 {title}: **{price} {cur}** → [View]({url})\n"
            else:
                msg += f"🔻 {title}: **{old} → {price} {cur}** → [View]({url})\n"
        send_to_discord(msg)
        print("Sent Discord update.")
    else:
        print("No changes found.")

    save_current(current)

if __name__ == "__main__":
    main()
