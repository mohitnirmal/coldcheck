#!/usr/bin/env python3
"""
Amazon + Flipkart price tracker.

Examples:
  python price_tracker.py --amazon-url "https://www.amazon.in/..." --flipkart-url "https://www.flipkart.com/..."
  python price_tracker.py --url "https://www.amazon.in/..." --url "https://www.flipkart.com/..." --interval 60
  python price_tracker.py --config price_targets.json

Optional Telegram push alerts:
  set TELEGRAM_BOT_TOKEN=123456:ABC...
  set TELEGRAM_CHAT_ID=123456789
  python price_tracker.py --url "https://www.amazon.in/..."

Example config file:
{
  "threshold": 550,
  "interval_seconds": 300,
  "products": [
    {"name": "Item on Amazon", "url": "https://www.amazon.in/..."},
    {"name": "Item on Flipkart", "url": "https://www.flipkart.com/..."}
  ]
}

Note: Amazon and Flipkart can change page markup or block automated requests.
This script alerts as soon as a polling check detects a price below the threshold.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from html import unescape
from typing import Iterable, Optional


DEFAULT_THRESHOLD = Decimal("550")
DEFAULT_INTERVAL_SECONDS = 300
DEFAULT_TIMEOUT_SECONDS = 20

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-IN,en;q=0.9",
    "Cache-Control": "no-cache",
}

PRICE_KEY_RE = re.compile(
    r"""
    ["']?
    (?:
        price|sellingPrice|finalPrice|offerPrice|dealPrice|discountedPrice|
        currentPrice|salePrice|amount
    )
    ["']?
    \s*[:=]\s*
    ["']?
    (?:\\u20b9|\u20b9|rs\.?|inr)?\s*
    ([0-9][0-9,]*(?:\.[0-9]{1,2})?)
    """,
    re.IGNORECASE | re.VERBOSE,
)

META_PRICE_RE = re.compile(
    r"""
    <meta
    (?=[^>]*(?:product:price:amount|og:price:amount|twitter:data1|price))
    [^>]*
    content=["'](?:\\u20b9|\u20b9|rs\.?|inr)?\s*
    ([0-9][0-9,]*(?:\.[0-9]{1,2})?)["']
    """,
    re.IGNORECASE | re.VERBOSE,
)

RUPEE_RE = re.compile(
    r"(?:\\u20b9|\u20b9|rs\.?|inr)\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)",
    re.IGNORECASE,
)

AMAZON_PRICE_WHOLE_RE = re.compile(
    r'class=["\'][^"\']*a-price-whole[^"\']*["\'][^>]*>\s*([0-9][0-9,]*)',
    re.IGNORECASE,
)

IMPORTANT_MARKERS = (
    "priceblock_ourprice",
    "priceblock_dealprice",
    "price_inside_buybox",
    "coreprice",
    "apex_desktop",
    "a-price-whole",
    "_30jeq3",
    "sellingprice",
    "finalprice",
    "dealprice",
)

GOOD_CONTEXT_WORDS = (
    "price",
    "deal",
    "sale",
    "selling",
    "final",
    "current",
    "buybox",
    "apex",
    "a-price",
    "_30jeq3",
    "ourprice",
)

BAD_CONTEXT_WORDS = (
    "mrp",
    "list price",
    "maximum retail",
    "was price",
    "strike",
    "regular price",
    "old price",
    "original price",
    "emi",
    "per month",
    "bank offer",
    "cashback",
    "coupon",
    "delivery",
    "shipping",
    "exchange",
    "save up to",
)

BLOCKED_PAGE_WORDS = (
    "captcha",
    "robot check",
    "enter the characters you see",
    "not a robot",
    "unusual traffic",
    "automated access",
)


@dataclass
class Product:
    name: str
    url: str
    threshold: Decimal
    was_below_threshold: bool = False
    last_alert_price: Optional[Decimal] = None


@dataclass
class PriceCandidate:
    price: Decimal
    score: int
    source: str
    order: int


def parse_money(value: object) -> Decimal:
    raw = str(value).strip().replace(",", "")
    try:
        amount = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError(f"Invalid amount: {value!r}") from exc
    if amount <= 0:
        raise ValueError(f"Amount must be positive: {value!r}")
    return amount


def format_money(amount: Decimal) -> str:
    if amount == amount.to_integral_value():
        return f"Rs {int(amount):,}"
    return f"Rs {amount:,.2f}"


def short_name_from_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc.replace("www.", "") or "product"
    return host


def fetch_html(url: str, timeout: int) -> str:
    request = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


def normalize_html(html: str) -> str:
    text = html.replace("\\u20b9", "\u20b9")
    text = text.replace("&#8377;", "\u20b9")
    return unescape(text)


def looks_blocked(html: str) -> bool:
    lowered = html.lower()
    return any(term in lowered for term in BLOCKED_PAGE_WORDS)


def context_score(context: str, base: int) -> int:
    lowered = context.lower()
    score = base
    score += 35 * sum(1 for word in GOOD_CONTEXT_WORDS if word in lowered)
    score -= 45 * sum(1 for word in BAD_CONTEXT_WORDS if word in lowered)
    return score


def add_candidate(
    candidates: list[PriceCandidate],
    raw_amount: str,
    source: str,
    order: int,
    context: str,
    base_score: int,
) -> None:
    try:
        price = parse_money(raw_amount)
    except ValueError:
        return
    if price > Decimal("10000000"):
        return
    score = context_score(context, base_score)
    candidates.append(PriceCandidate(price=price, score=score, source=source, order=order))


def extract_price(html: str) -> Optional[PriceCandidate]:
    page = normalize_html(html)
    candidates: list[PriceCandidate] = []
    order = 0

    for match in META_PRICE_RE.finditer(page):
        order += 1
        add_candidate(candidates, match.group(1), "meta price", order, match.group(0), 95)

    for match in PRICE_KEY_RE.finditer(page):
        order += 1
        start, end = match.span()
        context = page[max(0, start - 180) : min(len(page), end + 180)]
        add_candidate(candidates, match.group(1), "structured price", order, context, 85)

    for match in AMAZON_PRICE_WHOLE_RE.finditer(page):
        order += 1
        start, end = match.span()
        context = page[max(0, start - 260) : min(len(page), end + 260)]
        add_candidate(candidates, match.group(1), "amazon visible price", order, context, 90)

    lower_page = page.lower()
    for marker in IMPORTANT_MARKERS:
        start_at = 0
        while True:
            index = lower_page.find(marker, start_at)
            if index == -1:
                break
            window = page[max(0, index - 600) : min(len(page), index + 1000)]
            for match in RUPEE_RE.finditer(window):
                order += 1
                context_start, context_end = match.span()
                context = window[max(0, context_start - 180) : min(len(window), context_end + 180)]
                add_candidate(
                    candidates,
                    match.group(1),
                    f"price near {marker}",
                    order,
                    context,
                    80,
                )
            start_at = index + len(marker)

    for match in RUPEE_RE.finditer(page):
        order += 1
        start, end = match.span()
        context = page[max(0, start - 180) : min(len(page), end + 180)]
        add_candidate(candidates, match.group(1), "visible INR price", order, context, 35)
        if order > 250:
            break

    if not candidates:
        return None

    candidates.sort(key=lambda item: (-item.score, item.order, item.price))
    best = candidates[0]
    if best.score < 20:
        return None
    return best


def send_telegram(message: str) -> bool:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return False

    payload = urllib.parse.urlencode(
        {
            "chat_id": chat_id,
            "text": message,
            "disable_web_page_preview": "false",
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            response.read()
        return True
    except Exception as exc:  # noqa: BLE001 - notification failure should not stop monitoring
        print(f"[warn] Telegram alert failed: {exc}", file=sys.stderr)
        return False


def send_desktop_notification(title: str, message: str) -> bool:
    try:
        from plyer import notification  # type: ignore

        notification.notify(title=title, message=message, timeout=10)
        return True
    except Exception:
        return False


def beep() -> None:
    if platform.system().lower() == "windows":
        try:
            import winsound

            winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
            return
        except Exception:
            pass
    print("\a", end="", flush=True)


def notify(product: Product, price: Decimal, source: str) -> None:
    title = "Price alert"
    message = (
        f"{product.name}\n"
        f"Now: {format_money(price)}\n"
        f"Target: below {format_money(product.threshold)}\n"
        f"Source: {source}\n"
        f"{product.url}"
    )
    print("\n" + "=" * 72)
    print(title.upper())
    print(message)
    print("=" * 72 + "\n")
    beep()
    send_desktop_notification(title, message)
    send_telegram(f"{title}\n\n{message}")


def threshold_hit(price: Decimal, threshold: Decimal, inclusive: bool) -> bool:
    return price <= threshold if inclusive else price < threshold


def check_product(product: Product, timeout: int, inclusive: bool, verbose: bool) -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        html = fetch_html(product.url, timeout)
    except urllib.error.HTTPError as exc:
        print(f"[{timestamp}] {product.name}: HTTP {exc.code} while fetching {product.url}")
        return
    except urllib.error.URLError as exc:
        print(f"[{timestamp}] {product.name}: network error: {exc.reason}")
        return
    except Exception as exc:  # noqa: BLE001 - continue monitoring other products
        print(f"[{timestamp}] {product.name}: fetch failed: {exc}")
        return

    if looks_blocked(html):
        print(
            f"[{timestamp}] {product.name}: site returned a bot/CAPTCHA page; "
            "try a longer interval or check the URL in a browser."
        )
        return

    candidate = extract_price(html)
    if candidate is None:
        print(f"[{timestamp}] {product.name}: could not detect a reliable price.")
        return

    price = candidate.price
    comparison = "<=" if inclusive else "<"
    print(
        f"[{timestamp}] {product.name}: {format_money(price)} "
        f"(alert when {comparison} {format_money(product.threshold)})"
    )
    if verbose:
        print(f"  detected from: {candidate.source}, confidence score: {candidate.score}")

    is_below = threshold_hit(price, product.threshold, inclusive)
    already_alerted_for_this_price = product.last_alert_price == price
    should_alert = is_below and (
        not product.was_below_threshold or not already_alerted_for_this_price
    )

    if should_alert:
        notify(product, price, candidate.source)
        product.last_alert_price = price

    product.was_below_threshold = is_below


def load_config(path: Optional[str]) -> dict:
    if not path:
        return {}
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def products_from_config(config: dict, default_threshold: Decimal) -> list[Product]:
    products: list[Product] = []
    for entry in config.get("products", []):
        url = str(entry.get("url", "")).strip()
        if not url:
            continue
        threshold = parse_money(entry.get("threshold", default_threshold))
        products.append(
            Product(
                name=str(entry.get("name") or short_name_from_url(url)),
                url=url,
                threshold=threshold,
            )
        )
    return products


def products_from_urls(urls: Iterable[str], threshold: Decimal) -> list[Product]:
    products: list[Product] = []
    for url in urls:
        url = url.strip()
        if not url:
            continue
        products.append(Product(name=short_name_from_url(url), url=url, threshold=threshold))
    return products


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Track Amazon/Flipkart prices in INR.")
    parser.add_argument("--url", action="append", default=[], help="Product URL. Repeat as needed.")
    parser.add_argument("--amazon-url", action="append", default=[], help="Amazon product URL.")
    parser.add_argument("--flipkart-url", action="append", default=[], help="Flipkart product URL.")
    parser.add_argument("--config", help="JSON config file with products.")
    parser.add_argument(
        "--threshold",
        help="Global alert threshold in INR. Default: 550.",
    )
    parser.add_argument(
        "--interval",
        type=int,
        help=f"Seconds between checks. Default: {DEFAULT_INTERVAL_SECONDS}.",
    )
    parser.add_argument(
        "--inclusive",
        action="store_true",
        help="Alert when price is less than or equal to the threshold.",
    )
    parser.add_argument("--once", action="store_true", help="Check once and exit.")
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"HTTP timeout in seconds. Default: {DEFAULT_TIMEOUT_SECONDS}.",
    )
    parser.add_argument("--verbose", action="store_true", help="Show price detection details.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = load_config(args.config)

    threshold = parse_money(args.threshold or config.get("threshold", DEFAULT_THRESHOLD))
    interval = args.interval or int(config.get("interval_seconds", DEFAULT_INTERVAL_SECONDS))
    if interval < 30:
        print("[warn] Very short intervals can trigger anti-bot pages. Consider 60 seconds or more.")

    urls = []
    urls.extend(args.url)
    urls.extend(args.amazon_url)
    urls.extend(args.flipkart_url)

    products = products_from_config(config, threshold)
    products.extend(products_from_urls(urls, threshold))

    if not products:
        print("Add at least one product URL.")
        print(
            'Example: python price_tracker.py --amazon-url "https://www.amazon.in/..." '
            '--flipkart-url "https://www.flipkart.com/..."'
        )
        return 2

    print(f"Tracking {len(products)} product(s). Alert threshold: below {format_money(threshold)}.")
    print(f"Check interval: {interval} seconds. Press Ctrl+C to stop.")
    if os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID"):
        print("Telegram alerts: enabled.")
    else:
        print("Telegram alerts: disabled. Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID to enable.")

    try:
        while True:
            for product in products:
                check_product(product, args.timeout, args.inclusive, args.verbose)
            if args.once:
                break
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
