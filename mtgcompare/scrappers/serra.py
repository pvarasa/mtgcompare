"""Cardshop Serra (cardshop-serra.com) MTG scraper.

ec-cube platform. The product list page returns one card per (language ×
printing) — conditions (NM, NM-, EX, EX-, GD) are sub-rows of a price
table inside each card, with their own price and stock.

Title format:

    (<lang_jp>)<JP> / <EN>[ <flavor>] 【<SET>】[ No.<num>]

For example::

    (英)意志の力 / Force of Will【2XM】 No.051
    (英)意志の力 / Force of Will ★拡張枠★ 【DMR】 No.418
    (日)意志の力 / Force of Will【2XM】 No.051

We filter to ``(英)`` (English), NM-condition, in-stock rows.

The ``parse_search_html`` function is pure and is what tests exercise.
"""
import re

from bs4 import BeautifulSoup

from ._base import HtmlSearchScrapper

BASE_URL = "https://cardshop-serra.com"
SEARCH_URL = f"{BASE_URL}/mtg/products/list"

_PRICE_RE = re.compile(r"([\d,]+)\s*円")
# Stock count is rendered as "/N" next to the quantity input.
_STOCK_RE = re.compile(r"/\s*(\d+)")
# Title parser. Greedy on the JP side, non-greedy on the EN side so optional
# flavor markers like ★拡張枠★ stay outside the captured EN name.
_TITLE_RE = re.compile(
    r"^\((?P<lang>[^)]+)\)"
    r"(?P<jp>[^/]+?)"
    r"\s*/\s*"
    r"(?P<en>.+?)"
    r"\s*【(?P<set>[^】]+)】"
    r"(?:\s*No\.[\w\-]+)?"
    r"\s*$"
)
# Decorations sometimes appended to the EN name to flag printing variants.
_FLAVOR_RE = re.compile(r"\s*[★●■▼◆☆][^★●■▼◆☆]*[★●■▼◆☆]\s*$")


def _row_price_jpy(price_td) -> float | None:
    """Strip the (often-present) strike-through retail span before parsing."""
    strike = price_td.select_one(".product-list__item__table--price-original")
    if strike is not None:
        strike.extract()
    m = _PRICE_RE.search(price_td.get_text())
    return float(m.group(1).replace(",", "")) if m else None


def _row_stock(count_td) -> int:
    """0 if the row's count cell shows the "none" placeholder."""
    if count_td.select_one(".product-list__item__table--none"):
        return 0
    m = _STOCK_RE.search(count_td.get_text())
    return int(m.group(1)) if m else 0


def parse_search_html(html: str, card_name: str, fx_jpy_per_usd: float) -> list[dict]:
    """Extract NM English in-stock rows for ``card_name`` from a Serra page."""
    soup = BeautifulSoup(html, "html.parser")
    target = card_name.strip().lower()
    records: list[dict] = []

    for item in soup.select("div.product-list__item"):
        title_el = item.select_one("a.product-list__item__title--name")
        if title_el is None:
            continue

        title = re.sub(r"\s+", " ", title_el.get_text(" ", strip=True)).strip()
        m = _TITLE_RE.match(title)
        if not m:
            continue

        if m.group("lang") != "英":
            continue

        en = _FLAVOR_RE.sub("", m.group("en")).strip()
        if en.lower() != target:
            continue

        link = (title_el.get("href") or "").strip()
        if link and not link.startswith("http"):
            link = f"{BASE_URL}{link}"

        # Each card has a price table; one row per condition grade.
        for row in item.select("table.product-list__item__table tr"):
            type_th = row.select_one("th.product-list__item__table--type")
            price_td = row.select_one("td.product-list__item__table--price")
            count_td = row.select_one("td.product-list__item__table--count")
            if not (type_th and price_td and count_td):
                continue

            condition = type_th.get_text(strip=True)
            # Serra grades NM-/EX/EX-/GD as distinct from NM. Match Card Rush
            # convention of treating only NM as near-mint for our records.
            if condition != "NM":
                continue

            stock = _row_stock(count_td)
            if stock <= 0:
                continue

            price_jpy = _row_price_jpy(price_td)
            if price_jpy is None or price_jpy <= 0:
                continue

            records.append({
                "shop": "Cardshop Serra",
                "card": en,
                "set": m.group("set").strip(),
                "price_jpy": price_jpy,
                "price_usd": round(price_jpy / fx_jpy_per_usd, 2),
                "stock": stock,
                "condition": "NM",
                "link": link,
            })
    return records


class CardshopSerraScrapper(HtmlSearchScrapper):
    SHOP_NAME = "Cardshop Serra"
    SEARCH_URL = SEARCH_URL
    LOGGER_NAME = "mtgcompare.scrappers.serra"
    SEARCH_PARAM_NAME = "name"

    def parse_html(self, html: str, card_name: str) -> list[dict]:
        return parse_search_html(html, card_name, self.fx)
