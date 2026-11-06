"""Listing cells of the ocnk.net shop template (Card Rush, SingleStar).

Both shops' search pages, and SingleStar's sealed category pages, render
each product as an ``li.list_item_cell`` with the same name / price / stock
/ link nodes. ``iter_cells`` reads those; each caller parses the title.
"""
from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass

from selectolax.lexbor import LexborHTMLParser as HTMLParser
from selectolax.lexbor import LexborNode

# "9,930円".
_PRICE_RE = re.compile(r"([\d,]+)\s*円")
# "在庫数 4点" (SingleStar) / "在庫数 4枚" (Card Rush).
_STOCK_RE = re.compile(r"在庫数\s*(\d+)")


@dataclass(frozen=True)
class OcnkCell:
    node: LexborNode
    name: LexborNode
    link: str
    price_jpy: float | None
    stock: int  # 0 when sold out or not shown


def _stock(node: LexborNode | None) -> int:
    if node is None or "soldout" in (node.attributes.get("class") or "").split():
        return 0
    m = _STOCK_RE.search(node.text(deep=True, separator=" ", strip=True))
    return int(m.group(1)) if m else 0


def iter_cells(html: str | bytes, base_url: str) -> Iterator[OcnkCell]:
    """Every listing cell with a name and a link; relative links are made
    absolute against ``base_url``."""
    for cell in HTMLParser(html).css("li.list_item_cell"):
        name_el = cell.css_first("span.goods_name")
        link_el = cell.css_first("a.item_data_link")
        if not (name_el and link_el):
            continue
        price_el = cell.css_first("span.figure")
        price_m = _PRICE_RE.search(price_el.text(deep=True, separator=" ", strip=True)) if price_el else None
        href = (link_el.attributes.get("href") or "").strip()
        yield OcnkCell(
            node=cell,
            name=name_el,
            link=href if href.startswith("http") else f"{base_url}{href}",
            price_jpy=float(price_m.group(1).replace(",", "")) if price_m else None,
            stock=_stock(cell.css_first("p.stock")),
        )
