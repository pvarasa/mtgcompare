"""Phase 2 sealed tracking: Japanese shop titles, crawlers' parsers, offer
selection, the shop refresh, and JP-lot valuation. Offline; fixtures are
real shop output captured 2026-11-02."""
import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from mtgcompare import db
from mtgcompare.pricing import meta
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
