# Shop Integration Plan

Status snapshot of which Japanese MTG shops are integrated into mtgcompare, which ones could be added next, and the effort involved.

The candidate list comes from the shops that **Wisdom Guild's WONDER** price aggregator (`wonder.wisdom-guild.net`) tracks — the de-facto "shop universe" for the Japanese MTG market.

## Currently integrated

- Hareruya (`mtgcompare/scrappers/hareruya.py`)
- SingleStar — シングルスター (`mtgcompare/scrappers/singlestar.py`)
- Card Rush — カードラッシュ (`mtgcompare/scrappers/cardrush.py`) — first ocnk.net shop
- TokyoMTG (`mtgcompare/scrappers/tokyomtg.py`) — *not on the WONDER list, separate integration*

Of the 26 shops surfaced by WONDER, we already have 3 (Hareruya, SingleStar, Card Rush). The remaining 23 are the candidates below.

## Wisdom Guild itself — not a viable backend

- No public API, data feed, RSS, or sitemap (`/api`, `/data`, `/json`, `/rss`, `/sitemap.xml` all 404).
- Outbound link redirector (`link.php`) is gated behind **AWS WAF + CAPTCHA** — they actively defend against bots.
- ToS at `wisdom-guild.net/welcome/` explicitly prohibits real-time query proxying without prior admin consent (`webmaster@wisdom-guild.net`); allows once-daily bulk caching for personal use only.
- Their "最終チェック" / "最終更新" dual-timestamp model strongly implies they scrape participating shops on a schedule with the shops' opt-in consent — not a feed-based partnership.

Conclusion: integrate with the underlying shops directly. Don't proxy WONDER.

## Integration tiers

The 24 unintegrated shops cluster onto 4 e-commerce platforms plus a few customs. Most are scrapable today with the same `requests + BeautifulSoup` pattern used in `singlestar.py`.

### Tier 1 — Easy (~½ day per shop, less with shared base classes)

**ColorMe Shop** platform — search at `/?mode=srh&keyword=…` (or `/shop/shopbrand.html?search=…`). EUC-JP encoded.

| Shop | URL |
|---|---|
| BLACK FROG | https://blackfrog.jp/ |
| CARDMAX | https://www.cardmax.jp/ |
| Gemutlich | https://www.mtg-gemutlich.shop/ |
| MTG Guild | https://mtg-guild.com/ |
| TCG SHOP Suzunone | https://tcgshop-suzunone.com/ |

**ec-cube** platform — search at `/products/list?name=…`. UTF-8.

| Shop | URL |
|---|---|
| まんぞく屋 | https://shopmanzokuya.com/ |
| MINT MALL | https://www.mint-mall.net/ |
| カードショップ抜忍 | https://nukeninmtg.com/ |
| Cardshop Serra | https://cardshop-serra.com/mtg |

**ocnk.net** platform — search at `/product-list?keyword=…`. UTF-8.

| Shop | URL |
|---|---|
| ~~カードラッシュ~~ | ~~https://www.cardrush-mtg.jp/~~ — **integrated** |
| Ｆの集会場 | https://www.f-conclave.net/ |
| ゲームプラザ元気302 | https://www.genki302.com/ |
| CARDSHOP黒枠 | https://www.kurowaku.com/ |
| ゲームショップとど | https://todo.ocnk.net/ |

**Other server-rendered customs** — UTF-8, simple search URLs:

| Shop | URL | Search pattern |
|---|---|---|
| ENNDAL GAMES | https://www.enndalgames.com/ | `/?mode=srh&keyword=…` |
| カードショップはま屋 | https://www.cardshophamaya.com/ | `/?mode=srh&keyword=…` |
| 高岡サブカルチャーズ | https://shop.takaoka-sc.com/ | BASE platform: `/items?q=…` |
| GOODGAME | https://goodgame.co.jp/ | Shopify: `/search?q=…` |

**Note:** MINT MALL is a multi-tenant marketplace (it hosts MINT GAMES MTG and others) — one scraper covers multiple physical shops, which makes it the highest ROI in this tier.

### Tier 2 — Medium (~1 day per shop)

| Shop | URL | Quirk |
|---|---|---|
| HOBBY SHOPファミコンくん | https://www.arrive.co.jp/ | Old CGI, **Shift-JIS** encoded, search only matches Japanese names. Need JP↔EN name table (we already build this for SingleStar/Hareruya). MTG category id is `kis=2`. |
| トレトク | https://www.toretoku.jp/ | Search works at `/item?keyword=…&genre=4`, but English-MTG single-card coverage looks thin in probes — verify inventory volume before investing. |

### Tier 3 — Hard (Cloudflare bot challenge)

| Shop | URL | Notes |
|---|---|---|
| BIGWEB | https://mtg.bigweb.co.jp/ | Returns 403 with Cloudflare "Just a moment…" JS challenge. Needs Playwright or `curl_cffi`/`cloudscraper`. Major shop, worth the work. |
| ドラゴンスター | https://dorasuta.jp/mtg | Same Cloudflare protection. Also a major shop. |

These two would mean introducing a headless browser dependency or a TLS-fingerprint-faking HTTP client — a meaningful infrastructure decision (slower, heavier containers, harder CI). If pursued, do them together behind one shared anti-bot HTTP-client wrapper.

### Skip — no online store found

| Shop | Reason |
|---|---|
| スプーキードラゴン | No findable e-commerce site; physical-store only. |
| MTG専門店BellSearch | Couldn't locate their site. Possibly defunct or marketplace-only. |

## Suggested implementation order

1. **Refactor: shared platform base classes.** Add `mtgcompare/scrappers/_platforms/{colorme,eccube,ocnk}.py`. Each subclass per shop should be ~30 lines: shop name, base URL, optional field-extractor overrides. SingleStar already has the bilingual-name parsing logic that maps cleanly to all three platforms.
2. **MINT MALL first** for multi-shop leverage in one scraper.
3. **Roll out the rest of Tier 1** opportunistically — each one is largely a config change after the base classes exist.
4. **BIGWEB + ドラゴンスター** together once a decision is made on anti-bot HTTP infra.
5. **Famicomkun + Toretoku** later — moderate effort, narrower English-card payoff.

## Notes for whoever picks this up

- Most Japanese shops list cards as `【英語版】<JP name>/<EN name> [<SET>-<color/rarity>]`. The `_clean_english_name` regex pattern in `singlestar.py:46` is a good starting point.
- Currency: all shops price in JPY; the existing `utils.get_fx("jpy")` path is reused.
- Condition: most shops only sell NM English; a few (Toretoku, Cardrush) grade S/A/B/C/D — decide whether to filter to NM or surface grade in records.
- Cloudflare-gated shops have separate considerations for *bulk* indexing (see "Scrape-on-search vs daily bulk" below).
