"""Phase 2 sealed tracking: Japanese shop titles, crawlers' parsers, offer
selection, the shop refresh, and JP-lot valuation. Offline; fixtures are
real shop output captured 2026-11-02."""
import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from mtgcompare import db, meta
from mtgcompare.sealed import jp_shops, repo, service
from mtgcompare.sealed import jp_titles as jt
from mtgcompare.sealed import tcgplayer as tcg

FIXTURES = Path(__file__).parent / "fixtures"
TODAY = date(2026, 11, 2)


def _p(uuid, set_code, set_name, name, category, subtype):
    return {"uuid": uuid, "set_code": set_code, "set_name": set_name, "name": name,
            "category": category, "subtype": subtype, "release_date": "2026-01-01",
            "tcgplayer_product_id": None, "updated_at": "t"}


CATALOG = [
    _p("fra-play-box", "FRA", "Reality Fracture", "Reality Fracture Play Booster Box", "booster_box", "play"),
    _p("fra-play-pack", "FRA", "Reality Fracture", "Reality Fracture Play Booster Pack", "booster_pack", "play"),
    _p("fra-coll-box", "FRA", "Reality Fracture", "Reality Fracture Collector Booster Box", "booster_box", "collector"),
    _p("fra-coll-pack", "FRA", "Reality Fracture", "Reality Fracture Collector Booster Pack", "booster_pack", "collector"),
    _p("fra-sample", "FRA", "Reality Fracture", "Reality Fracture Collector Booster Sample Pack",
       "booster_pack", "collector"),
    _p("war-box", "WAR", "War of the Spark", "War of the Spark Booster Box", "booster_box", "draft"),
    _p("war-jp-box", "WAR", "War of the Spark", "War of the Spark Japanese Booster Box", "booster_box", "draft"),
    _p("war-pack", "WAR", "War of the Spark", "War of the Spark Booster Pack", "booster_pack", "draft"),
    _p("10e-box", "10E", "Tenth Edition", "Tenth Edition Booster Box", "booster_box", "draft"),
    _p("3ed-box", "3ED", "Revised Edition", "Revised Edition Booster Box", "booster_box", "default"),
    _p("fdn-play-box", "FDN", "Foundations", "Foundations Play Booster Box", "booster_box", "play"),
    _p("j25-box", "J25", "Foundations Jumpstart", "Foundations Jumpstart Booster Box", "booster_box", "jumpstart"),
    _p("mat-epi-box", "MAT", "March of the Machine: The Aftermath",
       "March of the Machine The Aftermath Epilogue Booster Box", "booster_box", "other"),
    _p("hob-play-box", "HOB", "The Hobbit", "The Hobbit Play Booster Box", "booster_box", "play"),
    _p("hob-play-pack", "HOB", "The Hobbit", "The Hobbit Play Booster Pack", "booster_pack", "play"),
    _p("ust-box", "UST", "Unstable", "Unstable Booster Box", "booster_box", "draft"),
    _p("ust-pack", "UST", "Unstable", "Unstable Booster Pack", "booster_pack", "draft"),
    _p("iko-draft-pack", "IKO", "Ikoria: Lair of Behemoths", "Ikoria Lair of Behemoths Draft Booster Pack",
       "booster_pack", "draft"),
    _p("iko-coll-pack", "IKO", "Ikoria: Lair of Behemoths", "Ikoria Lair of Behemoths Collector Booster Pack",
       "booster_pack", "collector"),
    _p("tmt-play-box", "TMT", "Teenage Mutant Ninja Turtles", "Teenage Mutant Ninja Turtles Play Booster Box",
       "booster_box", "play"),
]


def _by_set(rows=CATALOG):
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r["set_code"], []).append(r)
    return out


def _hareruya_listings():
    docs = json.loads((FIXTURES / "hareruya_sealed_docs.json").read_text(encoding="utf-8"))["docs"]
    return jp_shops.hareruya_listings_from_docs(docs)


def _singlestar_listings():
    return jp_shops.parse_singlestar_category_html((FIXTURES / "singlestar_sealed_category.html").read_bytes())


def _serra_listings():
    products = json.loads((FIXTURES / "serra_sealed_products.json").read_text(encoding="utf-8"))["products"]
    return jp_shops.serra_listings_from_products(products)


def _buylist_listings():
    return jp_shops.parse_hareruya_buylist_html((FIXTURES / "hareruya_buylist.html").read_bytes())


# --- titles --------------------------------------------------------------------

@pytest.mark.parametrize("title,expected", [
    ("(30パック)《リアリティ・フラクチャー プレイ・ブースターBOX》《○日本語版》[FRA]",
     ("FRA", "リアリティ・フラクチャー", "プレイ", "booster_box", "JP")),
    ("(1パック)《灯争大戦ブースターパック〇日本語版》[WAR]", ("WAR", "灯争大戦", None, "booster_pack", "JP")),
    ("(36パック)《灯争大戦ブースターBOX ●英語版》[WAR]", ("WAR", "灯争大戦", None, "booster_box", "EN")),
    ("【EN】(36パック)《リバイズド ブースターBOX ●英語版》[3ED]", ("3ED", "リバイズド", None, "booster_box", "EN")),
    # Hareruya's 10ED is MTGJSON's 10E.
    ("(36パック)《第10版ブースターBOX ●英語版》[10ED]", ("10E", "第10版", None, "booster_box", "EN")),
    ("(24パック)《機械兵団の進軍：決戦の後に エピローグ・ブースターBOX》《○日本語版》[MAT]",
     ("MAT", "機械兵団の進軍：決戦の後に", "エピローグ", "booster_box", "JP")),
])
def test_parse_hareruya_title(title, expected):
    got = jt.parse_hareruya_title(title)
    assert got is not None
    assert (got.set_code, got.set_name, got.booster_type, got.category, got.language) == expected


@pytest.mark.parametrize("title", [
    "【IT】(36パック)【白枠】《リバイズド ブースターBOX イタリア語版》[3ED]",   # neither EN nor JP
    "《歯車式司書/Cogwork Librarian》[CNS] 茶C",                               # a single card
    "(36パック)《ゼンディカーの夜明け ドラフト・ブースターBOX》《ロシア語版》[ZNR]",
])
def test_parse_hareruya_title_rejects(title):
    assert jt.parse_hareruya_title(title) is None


@pytest.mark.parametrize("title,expected", [
    ("リアリティ・フラクチャー 英語版 プレイブースター 1BOX", ("リアリティ・フラクチャー", "プレイ", "booster_box", "EN")),
    ("ホビット 日本語版 プレイブースター 1パック", ("ホビット", "プレイ", "booster_pack", "JP")),
    ("機械兵団の進軍：決戦の後に 英語版 エピローグブースター 1BOX",
     ("機械兵団の進軍：決戦の後に", "エピローグ", "booster_box", "EN")),
])
def test_parse_singlestar_title(title, expected):
    got = jt.parse_singlestar_title(title)
    assert got is not None
    assert (got.set_name, got.booster_type, got.category, got.language) == expected


@pytest.mark.parametrize("title", [
    "リアリティ・フラクチャー 英語版 シークレットレアー・バンドル 1BOX",
    "リアリティ・フラクチャー 英語版 統率者デッキ MULTIVERSE REFORGED 1BOX",
    "ホビット 日本語版 プレイブースター1BOX《訳あり未開封》 シュリンク破れ有",
    "モダンホライゾン3 日本語版 プレイブースター 10パックセット 1BOX",
    "スター・トレック 英語版プレイブースター3BOX (予約G)",
])
def test_parse_singlestar_title_rejects(title):
    assert jt.parse_singlestar_title(title) is None


def test_set_name_key_folds_width_and_crossover_prefix():
    assert jt.set_name_key("イニストラード:真紅の契り") == jt.set_name_key("イニストラード：真紅の契り")
    assert jt.set_name_key("マジック：ザ・ギャザリング | ホビット") == jt.set_name_key("ホビット")


def test_resolve_set_code_exact_prefix_and_ambiguous():
    names = {jt.set_name_key("ストリクスヘイヴン：魔法学院"): "STX", jt.set_name_key("灯争大戦"): "WAR"}
    assert jt.resolve_set_code("灯争大戦", names) == "WAR"
    assert jt.resolve_set_code("ストリクスヘイヴン", names) == "STX"   # SingleStar's short name
    names[jt.set_name_key("ストリクスヘイヴンの秘密")] = "SOS"
    assert jt.resolve_set_code("ストリクスヘイヴン", names) is None     # two candidates: don't guess


def _match(title, parse=jt.parse_hareruya_title, code=None):
    listing = parse(title)
    assert listing is not None
    return jt.match_product(listing, code or listing.set_code, _by_set())


def test_match_prefers_canonical_over_sample_pack():
    assert _match("(1パック)《リアリティ・フラクチャー コレクター・ブースターパック》《●英語版》[FRA]")["uuid"] == "fra-coll-pack"


def test_match_japanese_edition_only_for_jp_listings():
    assert _match("(36パック)《灯争大戦ブースターBOX 〇日本語版》[WAR]")["uuid"] == "war-jp-box"
    assert _match("(36パック)《灯争大戦ブースターBOX ●英語版》[WAR]")["uuid"] == "war-box"


def test_match_untyped_booster_falls_back_to_default_subtype():
    assert _match("【EN】(36パック)《リバイズド ブースターBOX ●英語版》[3ED]")["uuid"] == "3ed-box"
    assert _match("(36パック)《第10版ブースターBOX ●英語版》[10ED]")["uuid"] == "10e-box"


def test_match_jumpstart_filed_under_own_set_and_epilogue_as_other():
    assert _match("(24パック)《ファウンデーションズ ジャンプスタート・ブースターBOX》《●英語版》[FDN]")["uuid"] == "j25-box"
    assert _match("(24パック)《機械兵団の進軍：決戦の後に エピローグ・ブースターBOX》《○日本語版》[MAT]")["uuid"] == "mat-epi-box"


# --- shop parsers + offer selection --------------------------------------------

def test_hareruya_listings_from_docs():
    listings = _hareruya_listings()
    fra = next(x for x in listings if "プレイ・ブースターBOX》《○日本語版》[FRA]" in x.title)
    assert fra.price_jpy == 18900 and fra.stock == 48
    assert fra.url.startswith("https://www.hareruyamtg.com/ja/products/detail/")


def test_parse_singlestar_category_fixture():
    listings = _singlestar_listings()
    assert len(listings) == 15
    play_en = next(x for x in listings if x.title == "リアリティ・フラクチャー 英語版 プレイブースター 1BOX")
    assert play_en.price_jpy == 17800 and play_en.stock > 0
    assert play_en.hint_set_code == "FRA"
    assert play_en.url == "https://www.singlestar.jp/product/346534"


def test_parse_singlestar_sold_out_and_members_price():
    html = """<ul>
      <li class="list_item_cell"><a class="item_data_link" href="https://www.singlestar.jp/product/1">
        <span class="goods_name">ホビット 英語版 プレイブースター 1BOX</span></a>
        <span class="figure">25,800円</span><p class="stock soldout">在庫なし</p></li>
      <li class="list_item_cell"><a class="item_data_link" href="https://www.singlestar.jp/product/2">
        <span class="goods_name">ホビット 日本語版 プレイブースター 1BOX</span></a>
        <span class="figure">会員販売価格</span><p class="stock">在庫数 5点</p></li>
    </ul>"""
    sold_out, members = jp_shops.parse_singlestar_category_html(html)
    assert sold_out.stock == 0 and sold_out.price_jpy == 25800
    assert members.price_jpy is None and members.stock == 5


def test_best_offers_end_to_end_from_fixtures():
    hareruya = _hareruya_listings()
    names = jp_shops.hareruya_set_names(hareruya)
    h_offers, h_stats = jp_shops.best_offers(jp_shops.HARERUYA, hareruya, _by_set(), names)
    # In-stock FRA play boxes: JP ¥18,900 and EN ¥22,500. Out-of-stock ones are dropped.
    assert h_offers[("fra-play-box", "JP")].price_jpy == 18900
    assert h_offers[("fra-play-box", "EN")].price_jpy == 22500
    assert ("fra-coll-box", "EN") not in h_offers
    assert h_stats["parsed"] < h_stats["listings"]   # single cards / Italian rejected

    s_offers, _ = jp_shops.best_offers(jp_shops.SINGLESTAR, _singlestar_listings(), _by_set(), names)
    # SingleStar titles have no set code: resolved through Hareruya's names.
    assert s_offers[("fra-play-box", "EN")].price_jpy == 17800
    # ホビット isn't in the Hareruya fixture: the image-path code is the fallback.
    assert ("hob-play-box", "EN") in s_offers


# --- service -------------------------------------------------------------------

@pytest.fixture
def seeded(test_db):
    with db.get_conn() as conn:
        repo.upsert_products(conn, CATALOG)
    return test_db


def test_refresh_shop_prices_records_and_isolates_failures(seeded):
    def boom():
        raise RuntimeError("down")

    results = service.refresh_shop_prices(
        crawlers={jp_shops.HARERUYA: _hareruya_listings, jp_shops.SINGLESTAR: boom}, today=TODAY,
    )
    assert results[jp_shops.HARERUYA]["recorded"] > 0
    assert "error" in results[jp_shops.SINGLESTAR]
    with db.get_conn() as conn:
        pts = repo.latest_points(conn, ["fra-play-box"])
        names = json.loads(meta.read(conn, service.SET_NAMES_META_KEY))
    hj = pts[("fra-play-box", jp_shops.HARERUYA, "JP")]
    assert hj["price"] == 18900 and hj["currency"] == "JPY" and hj["stock"] == 48 and hj["url"]
    assert jt.set_name_key("リアリティ・フラクチャー") in names

    # Next day Hareruya is down: SingleStar still resolves via the stored names.
    results = service.refresh_shop_prices(
        crawlers={jp_shops.HARERUYA: boom, jp_shops.SINGLESTAR: _singlestar_listings},
        today=TODAY + timedelta(days=1),
    )
    with db.get_conn() as conn:
        pts = repo.latest_points(conn, ["fra-play-box"])
    assert pts[("fra-play-box", jp_shops.SINGLESTAR, "EN")]["price"] == 17800


def _point(uuid, source, language, day, price, currency):
    return {"product_uuid": uuid, "source": source, "language": language, "market_date": day,
            "price": price, "currency": currency, "url": f"https://shop/{source}", "stock": 3}


def test_valuation_basis_per_language(seeded):
    with db.get_conn() as conn:
        for lang in ("EN", "JP"):
            repo.add_lot(conn, "u", {"product_uuid": "fra-play-box", "language": lang, "quantity": 1,
                                     "price_bought": 15000, "currency": "JPY", "date_bought": None})
        repo.add_lot(conn, "u", {"product_uuid": "war-box", "language": "JP", "quantity": 1,
                                 "price_bought": None, "currency": "JPY", "date_bought": None})
        repo.add_watch(conn, "u", "fra-play-box", "t")
        repo.upsert_prices(conn, [
            _point("fra-play-box", tcg.SOURCE, "EN", TODAY, 120.0, "USD"),
            _point("fra-play-box", jp_shops.HARERUYA, "JP", TODAY, 18900, "JPY"),
            _point("fra-play-box", jp_shops.SINGLESTAR, "JP", TODAY, 17800, "JPY"),
            _point("fra-play-box", jp_shops.SINGLESTAR, "EN", TODAY, 17800, "JPY"),
            _point("fra-play-box", jp_shops.HARERUYA, "JP", TODAY - timedelta(days=30), 20000, "JPY"),
            _point("war-box", tcg.SOURCE, "EN", TODAY, 150.0, "USD"),
            # Last seen in stock 10 days ago → treated as sold out.
            _point("war-box", jp_shops.HARERUYA, "JP", TODAY - timedelta(days=10), 21600, "JPY"),
        ])
    ctx = service.compute_sealed_ctx("u", fx=150.0, today=TODAY)
    lots = {(r["uuid"], r["language"]): r for r in ctx["lots"]}

    en = lots[("fra-play-box", "EN")]
    assert en["basis_label"] == "TCGPlayer" and en["market_usd"] == 120.0
    assert en["offer"]["price"] == 17800 and en["offer"]["label"] == "SingleStar"
    assert not en["reference_mismatch"]

    jp = lots[("fra-play-box", "JP")]
    assert jp["basis_label"] == "SingleStar" and jp["market_jpy"] == 17800
    assert jp["market_usd"] == round(17800 / 150, 2)
    assert jp["series"] == "singlestar:JP"
    assert not jp["reference_mismatch"]

    war = lots[("war-box", "JP")]
    assert war["offer"] is None and war["basis_label"] == "TCGPlayer"
    assert war["reference_mismatch"]

    watch = ctx["watch"][0]
    assert watch["tcg_usd"] == 120.0
    assert watch["offer_en"]["price"] == 17800 and watch["offer_jp"]["price"] == 17800


def test_history_endpoint_series(seeded, monkeypatch):
    from mtgcompare import web
    with db.get_conn() as conn:
        repo.upsert_prices(conn, [
            _point("fra-play-box", tcg.SOURCE, "EN", TODAY, 120.0, "USD"),
            _point("fra-play-box", jp_shops.HARERUYA, "JP", TODAY, 18900, "JPY"),
        ])
    with web.app.test_client() as c:
        default = c.get("/sealed/history?uuid=fra-play-box").get_json()
        assert default["series"] == "tcgplayer:EN" and default["currency"] == "USD"
        assert {o["key"] for o in default["series_options"]} == {"tcgplayer:EN", "hareruya:JP"}
        shop = c.get("/sealed/history?uuid=fra-play-box&series=hareruya:JP").get_json()
        assert shop["currency"] == "JPY" and shop["points"][0]["price"] == 18900
        bogus = c.get("/sealed/history?uuid=fra-play-box&series=evil:XX").get_json()
        assert bogus["series"] == "tcgplayer:EN"


# --- phase 3: Cardshop Serra + Hareruya buylist ---------------------------------

@pytest.mark.parametrize("title,expected", [
    ("(日)灯争大戦 ブースターボックス / War of the Spark Booster Box【BOX】 ※発送方法は「ゆうパック」",
     ("灯争大戦", None, "booster_box", "JP", "War of the Spark Booster Box")),
    ("(英)Unstable ブースターボックス 【BOX】 ※発送方法は「ゆうパック」を選択してください。",
     ("Unstable", None, "booster_box", "EN", None)),
    ("(日)『マジック：ザ・ギャザリング--FINAL FANTASY』 プレイ・ブースターパック / Magic: The Gathering-FINAL FANTASY"
     " Play Booster Pack【PACK】", ("FINAL FANTASY", "プレイ", "booster_pack", "JP",
                                  "Magic: The Gathering-FINAL FANTASY Play Booster Pack")),
    ("(日)『ローウィンの昏明』コレクター・ブースターボックス /Lorwyn Eclipsed Collecter Booster Box【BOX】",
     ("ローウィンの昏明", "コレクター", "booster_box", "JP", "Lorwyn Eclipsed Collector Booster Box")),
])
def test_parse_serra_title(title, expected):
    p = jt.parse_serra_title(title)
    assert p is not None
    assert (p.set_name, p.booster_type, p.category, p.language, p.en_name) == expected


@pytest.mark.parametrize("title", [
    "(日)ファウンデーションズ プレイ・ブースターボックス 1カートン(6Box) / Foundations Play Booster Box 1 Carton(6 Boxes)【BOX】",
    "(日)ラヴニカへの回帰 ブースターバトルボックス / Return to Ravnica Booster Battle Box【BOX】",
    "(英)Secret Lair x Uncharted 【BOX】 ※発送方法は「ゆうパック」を選択してください。",
    "(イタリア語)ゼンディカー ブースターボックス / Zendikar Booster Box【BOX】",
    "(英)『マジック：ザ・ギャザリング | マーベル スパイダーマン』 バンドル / Marvel's Spider-Man Bundle【BOX】",
    "(日)スターター1999 ブリスター ブースターパック / Starter 1999 Blister Booster Pack【PACK】",
])
def test_parse_serra_title_rejects(title):
    assert jt.parse_serra_title(title) is None


def test_serra_listings_price_nm_variant_and_availability():
    by_title = {listing.title.split("【")[0]: listing for listing in _serra_listings()}
    ust = by_title["(英)Unstable ブースターボックス "]
    # NM ¥36,000 in stock; the SP/MP variants (damaged boxes) are ignored.
    assert ust.price_jpy == 36000 and ust.stock is None and ust.available
    assert ust.url.startswith("https://cardshop-serra.com/products/")
    war_en = by_title["(英)灯争大戦 ブースターボックス / War of the Spark Booster Box"]
    assert war_en.stock == 0 and not war_en.available


def test_serra_best_offers():
    names = {jt.set_name_key("イコリア：巨獣の棲処"): "IKO"}
    offers, stats = jp_shops.best_offers(jp_shops.SERRA, _serra_listings(), _by_set(), names)
    # English name → Japan-only edition for a JP listing.
    assert offers[("war-jp-box", "JP")].price_jpy == 28800
    assert ("war-box", "EN") not in offers                       # sold out
    # No English half: resolved by the English set name.
    assert offers[("ust-box", "EN")].price_jpy == 36000
    assert offers[("ust-pack", "EN")].price_jpy == 1000
    # The carton is rejected, so the single box keeps its own price.
    assert offers[("fdn-play-box", "JP")].price_jpy == 19800
    assert offers[("tmt-play-box", "JP")].price_jpy == 23100
    # Ikoria is sold out. Serra's English half drops "Collector"; the
    # Japanese コレクター still finds the right product.
    iko = jt.parse_serra_title(
        "(日)イコリア：巨獣の棲処 コレクター・ブースターパック / Ikoria: Lair of Behemoths Booster Pack【PACK】")
    assert iko is not None
    assert jt.match_product(iko, "IKO", _by_set())["uuid"] == "iko-coll-pack"
    assert ("iko-coll-pack", "JP") not in offers
    assert stats["parsed"] < stats["listings"]                   # Secret Lair skipped


def test_buylist_parse_and_lowest_bid_per_product():
    listings = _buylist_listings()
    assert all(listing.url.startswith("https://www.hareruyamtg.com/ja/purchase/detail/") for listing in listings)
    names = jp_shops.hareruya_set_names(listings)
    assert names[jt.set_name_key("灯争大戦")] == "WAR"
    offers, stats = jp_shops.best_offers(jp_shops.HARERUYA_BUY, listings, _by_set(), names)
    # Two bids on the JP box (first print ¥14,400, reprint ¥10,800): the
    # conservative one wins, since which printing a holder has is unknown.
    assert offers[("war-jp-box", "JP")].price_jpy == 10800
    assert offers[("war-box", "EN")].price_jpy == 10800
    assert offers[("fdn-play-box", "JP")].price_jpy == 3600
    assert offers[("war-pack", "JP")].price_jpy == 300
    assert stats["matched"] == 5                                 # FUT isn't in the test catalog


def test_buylist_on_lots_summary_and_history(seeded):
    with db.get_conn() as conn:
        repo.add_lot(conn, "u", {"product_uuid": "fra-play-box", "language": "JP", "quantity": 2,
                                 "price_bought": 15000, "currency": "JPY", "date_bought": None})
        repo.add_lot(conn, "u", {"product_uuid": "war-box", "language": "EN", "quantity": 1,
                                 "price_bought": None, "currency": "JPY", "date_bought": None})
        repo.add_watch(conn, "u", "fra-play-box", "t")
        repo.upsert_prices(conn, [
            _point("fra-play-box", jp_shops.SERRA, "JP", TODAY, 18000, "JPY"),
            _point("fra-play-box", jp_shops.HARERUYA_BUY, "JP", TODAY, 12000, "JPY"),
            _point("fra-play-box", jp_shops.HARERUYA_BUY, "EN", TODAY, 13000, "JPY"),
            # A bid not seen for 10 days: Hareruya stopped buying it.
            _point("war-box", jp_shops.HARERUYA_BUY, "EN", TODAY - timedelta(days=10), 9000, "JPY"),
        ])
    ctx = service.compute_sealed_ctx("u", fx=150.0, today=TODAY)
    lots = {r["uuid"]: r for r in ctx["lots"]}
    fra = lots["fra-play-box"]
    assert fra["basis_label"] == "Cardshop Serra" and fra["market_jpy"] == 18000
    assert fra["buy"]["price"] == 12000 and fra["buy"]["label"] == "Hareruya buylist"
    assert fra["buy_value_jpy"] == 24000
    assert lots["war-box"]["buy"] is None
    s = ctx["summary"]
    assert s["lots_with_buy"] == 1 and s["total_buy_jpy"] == 24000 and s["total_buy_usd"] == 160.0
    w = ctx["watch"][0]
    assert w["buy_jp"]["price"] == 12000 and w["buy_en"]["price"] == 13000

    assert service.parse_series("hareruya_buy:JP") == (jp_shops.HARERUYA_BUY, "JP")
    hist = service.product_history("fra-play-box", (jp_shops.HARERUYA_BUY, "JP"))
    assert hist is not None
    assert hist["currency"] == "JPY" and hist["points"][0]["price"] == 12000
    assert "what the shop pays" in hist["source_detail"]
    assert "Hareruya buylist (JP)" in {o["label"] for o in hist["series_options"]}


def test_buylist_teaches_set_names_for_serra(seeded):
    service.refresh_shop_prices(crawlers={
        jp_shops.HARERUYA_BUY: _buylist_listings,
        jp_shops.SERRA: _serra_listings,
    }, today=TODAY)
    with db.get_conn() as conn:
        names = json.loads(meta.read(conn, service.SET_NAMES_META_KEY))
        pts = repo.latest_points(conn, ["war-jp-box", "fdn-play-box"])
    assert jt.set_name_key("灯争大戦") in names
    assert pts[("war-jp-box", jp_shops.HARERUYA_BUY, "JP")]["price"] == 10800
    assert pts[("fdn-play-box", jp_shops.SERRA, "JP")]["price"] == 19800
    assert pts[("fdn-play-box", jp_shops.SERRA, "JP")]["stock"] is None


def test_english_name_match_vetoed_by_japanese_booster_type():
    index = jt.index_by_english_name(_by_set())
    plain = jt.JpListing(None, "灯争大戦", None, "booster_pack", "EN", en_name="War of the Spark Booster Pack")
    assert jt.match_english_name(plain, index)["uuid"] == "war-pack"
    collector = jt.JpListing(None, "灯争大戦", "コレクター", "booster_pack", "EN",
                             en_name="War of the Spark Booster Pack")
    assert jt.match_english_name(collector, index) is None
    box_for_pack = jt.JpListing(None, "灯争大戦", None, "booster_box", "EN", en_name="War of the Spark Booster Pack")
    assert jt.match_english_name(box_for_pack, index) is None


class _Resp:
    def __init__(self, status, body=None, headers=None):
        self.status_code, self._body, self.headers = status, body, headers or {}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"{self.status_code}")


class _Session:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), 0

    def get(self, url, params=None, timeout=None):
        self.calls += 1
        return self.responses.pop(0)


def test_crawl_serra_waits_out_one_429_then_fails_on_a_second():
    products = json.loads((FIXTURES / "serra_sealed_products.json").read_text(encoding="utf-8"))["products"]
    waits: list[float] = []
    ok = _Session([_Resp(429, headers={"Retry-After": "60"}), _Resp(200, {"products": products})])
    assert len(jp_shops.crawl_serra(ok, pacing_s=0, sleep=waits.append)) == len(products)
    assert waits == [60.0] and ok.calls == 2

    banned = _Session([_Resp(429, headers={"Retry-After": "600"}), _Resp(429)])
    with pytest.raises(jp_shops.ScraperFetchError):
        jp_shops.crawl_serra(banned, pacing_s=0, sleep=waits.append)
    assert waits[-1] == 90.0   # capped
