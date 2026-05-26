# Shop Integration Plan

Status snapshot of which Japanese MTG shops are integrated into mtgcompare, which ones could be added next, and the effort involved.

The candidate list comes from the shops that **Wisdom Guild's WONDER** price aggregator (`wonder.wisdom-guild.net`) tracks — the de-facto "shop universe" for the Japanese MTG market.

## Currently integrated

- Hareruya (`mtgcompare/scrapers/hareruya.py`)
- SingleStar — シングルスター (`mtgcompare/scrapers/singlestar.py`)
- Card Rush — カードラッシュ (`mtgcompare/scrapers/cardrush.py`) — first ocnk.net shop
- Cardshop Serra (`mtgcompare/scrapers/serra.py`) — first ec-cube shop, biggest indexed inventory (~1.19M)
- ENNDAL GAMES (`mtgcompare/scrapers/enndalgames.py`) — second-biggest indexed inventory (~493k); custom platform. **Still disabled in `build_scrapers`.** As of 2026-05-26 *public* resolvers recovered (`dig +short www.enndalgames.com @8.8.8.8` → `13.159.57.5` / `52.193.201.15`) and the live scraper parses NM-EN rows from a dev box — but the **prod cluster's upstream DNS still can't resolve `www.enndalgames.com`** (in-pod `socket.gethostbyname` → Errno -5; the apex `enndalgames.com` resolves to `219.94.128.207` but its TLS cert is www-only, so it's not a usable fallback). Re-enable only after an in-cluster resolution check passes — the dev-box `dig` is not sufficient evidence.
- BLACK FROG (`mtgcompare/scrapers/blackfrog.py`) — first ColorMe-platform shop (~119k indexed); legacy `/shop/shopbrand.html?search=…` URL, EUC-JP encoded
- MINT MALL (`mtgcompare/scrapers/mintmall.py`) — multi-tenant ec-cube marketplace (~90k indexed); per-spec stock + price come from a `specificationTreeSearchProductsTree` JS const, not the listing markup
- TokyoMTG (`mtgcompare/scrapers/tokyomtg.py`) — *not on the WONDER list, separate integration*

**Skipped (data quality):**

- **MTG Guild** (~135k indexed). Listing search and product detail pages neither expose set codes nor have any structured data, breadcrumbs, or JSON-LD from which to extract them. Without set info, our records would pollute price comparisons. Defer until the shop's listing format changes or until we add a set-code inference layer.

Of the 26 shops surfaced by WONDER, we already have 7 (Hareruya, SingleStar, Card Rush, Cardshop Serra, ENNDAL GAMES, BLACK FROG, MINT MALL). The remaining 19 are the candidates below.

## Wisdom Guild itself — not a viable backend

- No public API, data feed, RSS, or sitemap (`/api`, `/data`, `/json`, `/rss`, `/sitemap.xml` all 404).
- Outbound link redirector (`link.php`) is gated behind **AWS WAF + CAPTCHA** — they actively defend against bots.
- ToS at `wisdom-guild.net/welcome/` explicitly prohibits real-time query proxying without prior admin consent (`webmaster@wisdom-guild.net`); allows once-daily bulk caching for personal use only.
- Their "最終チェック" / "最終更新" dual-timestamp model strongly implies they scrape participating shops on a schedule with the shops' opt-in consent — not a feed-based partnership.

Conclusion: integrate with the underlying shops directly. Don't proxy WONDER.

## Measured volume (probe, 2026-05-26)

The tiers below were originally ranked by *platform and scraping effort*, not
by measured volume — only the integrated shops had inventory figures. To close
that gap, `scripts/probe_shop_volume.py` hits each shop's real search endpoint
for a basket of 8 bellwether staples (Force of Will, Brainstorm, Lightning
Bolt, Counterspell, Sol Ring, Swords to Plowshares, Birds of Paradise, Llanowar
Elves) and counts the **relevant** result cards — product-card nodes whose
bilingual title actually names the queried card. Re-run it any time:

```sh
uv run python scripts/probe_shop_volume.py --json docs/shop_volume_probe.json
```

**What the number means / caveats.** "med" is the median relevant listings per
staple — a *search-breadth-on-staples* proxy, not a catalog count and not
English-NM-in-stock depth (that needs the per-shop parser). It is good for
ranking candidates against each other and roughly against the anchors. Two
deliberate guards keep it honest: it counts only result-card nodes a known
platform selector matches (the page-wide 【SET】 token regex over-counts the
sidebar/footer "recommended items" rails by 10–45× and is diagnostic-only), and
it filters to cards whose title names the query (whitespace-insensitive, to
survive ocnk's `result_emphasis` keyword highlighting).

**Calibration anchors** (integrated shops, so "med" can be read against a known
catalog size):

| Shop | med listings/staple | known indexed |
|---|---:|---|
| Card Rush | 100 | (integrated) |
| SingleStar | 48 | (integrated) |
| BLACK FROG | 45 | ~119k |
| MINT MALL | 42 | ~90k |
| Cardshop Serra | 36 | ~1.19M |
| ENNDAL GAMES | 15 | ~493k |

Note med/staple is *not* proportional to total catalog (Serra's 1.19M is spread
broad-and-shallow; ~36/staple), so treat it as "is this shop in the same league
as ones we already ingest," which the anchors put at roughly **15–100**.

**Candidate ranking** (relevant med/staple, 8-card basket):

| Shop | platform | med | hit rate | verdict |
|---|---|---:|---|---|
| **GOODGAME** | Shopify | **95** | 8/8 | **Top pick.** Depth on par with Card Rush; clean `【EN】渦まく知識/Brainstorm [SET]` bilingual titles; trivial `/search?q=` endpoint. |
| Todo | ocnk | 16 | 8/8 | Workable. ENNDAL-band depth; ocnk base already exists → cheap. |
| Kurowaku | ocnk | 7 | 8/8 | Thin but real; cheap once ocnk base exists. |
| Suzunone | ColorMe | 7 | 7/8 | Real results in `.productlist_list` (modern ColorMe theme). |
| F-conclave | ocnk | 4 | 8/8 | Thin. |
| Genki302 | ocnk | 0 | 2/8 | English search mostly returns nothing. |
| Takaoka | BASE | — | — | **Fuzzy search**: `/items?q=` returns a whole MTG grid (24 cards/page) that ignores the query term — real depth unmeasured. Titles *are* clean bilingual MTG; worth a manual look, but `q=` isn't a usable single-card lookup. |
| Hamaya | ColorMe | — | — | Fuzzy search, only ~2 cards/page surfaced. |
| Manzokuya | ec-cube | 0 | 0/8 | **No English (or JP) card-node match** — search returns 0 shelf items even for 渦まく知識. Not an easy English-searchable shop. |
| Nukenin | ec-cube | 0 | 0/8 | No card-node match for English names. |
| CARDMAX | ColorMe | 0 | 0/8 | No card-node match for English names. |
| Gemutlich | ColorMe | 0 | 0/8 | No card-node match for English names. |

**Findings that revise the plan:**

1. **GOODGAME is the clear next integration** — it's the only candidate whose
   measured depth (95) lands in the integrated band, it speaks the same
   bilingual title format the existing parsers already handle, and Shopify
   `/search?q=` is the simplest endpoint of any candidate.
2. **The platform-based "easy" tiering was over-optimistic.** Manzokuya,
   Nukenin, CARDMAX, and Gemütlich return *zero* parseable products for English
   card names — their search needs Japanese names, i.e. a JP↔EN name table
   (the Tier-2 effort the plan only attributed to Famicomkun). They are **not**
   ½-day ColorMe/ec-cube config jobs.
3. **A clean `requests` GET ≠ a usable single-card search.** Takaoka (BASE) and
   Hamaya return HTTP 200 with MTG grids, but `q=`/`keyword=` doesn't actually
   filter to the queried card — so they can't back the per-card fan-out without
   a different endpoint.
4. **The ocnk candidates are the safe, cheap tier-1 tail** (Todo 16 > Kurowaku 7
   > F-conclave 4), all reusing the existing ocnk pattern; Genki302 is not worth
   it.

## Integration tiers

The 24 unintegrated shops cluster onto 4 e-commerce platforms plus a few customs. Most are scrapable today with the same `requests + selectolax` pattern used in `singlestar.py` (selectolax replaced BeautifulSoup project-wide in v1.6.8 for ~75% lower parse-tree memory).

### Tier 1 — Easy (~½ day per shop, less with shared base classes)

> ⚠️ The groupings below are by *platform*, which is no longer the same as
> *effort* — see "Measured volume" above. Several of these (Manzokuya, Nukenin,
> CARDMAX, Gemütlich) return nothing for English card names and actually belong
> in the name-table tier; GOODGAME (in "other customs") is the real easy win.

**ColorMe Shop** platform — search at `/?mode=srh&keyword=…` (or `/shop/shopbrand.html?search=…`). EUC-JP encoded.

| Shop | URL |
|---|---|
| ~~BLACK FROG~~ | ~~https://blackfrog.jp/~~ — **integrated** |
| CARDMAX | https://www.cardmax.jp/ |
| Gemutlich | https://www.mtg-gemutlich.shop/ |
| ~~MTG Guild~~ | ~~https://mtg-guild.com/~~ — **deferred** (no set codes in listing or detail) |
| TCG SHOP Suzunone | https://tcgshop-suzunone.com/ |

**ec-cube** platform — search at `/products/list?name=…`. UTF-8.

| Shop | URL |
|---|---|
| まんぞく屋 | https://shopmanzokuya.com/ |
| MINT MALL | https://www.mint-mall.net/ |
| カードショップ抜忍 | https://nukeninmtg.com/ |
| ~~Cardshop Serra~~ | ~~https://cardshop-serra.com/mtg~~ — **integrated** |
| ~~MINT MALL~~ | ~~https://www.mint-mall.net/~~ — **integrated** |

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
| ~~ENNDAL GAMES~~ | ~~https://www.enndalgames.com/~~ — **integrated** | `/products/list.php?mode=search&name=…` |
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

Revised after the 2026-05-26 volume probe (see "Measured volume" above).

0. **ENNDAL GAMES — blocked on cluster DNS.** Public DNS recovered but the
   prod cluster still can't resolve `www.enndalgames.com` (see above). Stays
   disabled until an in-pod resolution check passes; no app change needed then
   beyond flipping the flag.
1. **GOODGAME first** — the highest-volume candidate (≈95 med/staple, on par with
   Card Rush) and the simplest endpoint (Shopify `/search?q=`). Its
   `【EN】<JP>/<EN> [SET]` titles match the existing bilingual-name parsers.
2. **Refactor: shared platform base classes.** Add
   `mtgcompare/scrapers/_platforms/{ocnk,colorme,eccube}.py`. Each per-shop
   subclass is then ~30 lines. SingleStar's bilingual-name logic maps cleanly.
3. **ocnk tail behind the base class** — Todo (16) > Kurowaku (7) > F-conclave (4).
   Cheap config once the ocnk base exists. Skip Genki302 (English search empty).
4. **Suzunone** (modern ColorMe, results in `.productlist_list`, ≈7/staple).
5. **BIGWEB + ドラゴンスター** together once a decision is made on anti-bot HTTP infra.
6. **JP-name-table tier (was mis-filed as Tier 1):** Manzokuya, Nukenin, CARDMAX,
   Gemütlich return nothing for English names — they need a JP↔EN name table
   first, same as **Famicomkun**. Group them with Famicomkun + Toretoku as the
   name-table-dependent batch. Verify Takaoka/Hamaya have a query-respecting
   search endpoint before committing (their `q=`/`keyword=` returned unfiltered
   grids in the probe).

## Notes for whoever picks this up

- Most Japanese shops list cards as `【英語版】<JP name>/<EN name> [<SET>-<color/rarity>]`. The `_clean_english_name` regex pattern in `singlestar.py:46` is a good starting point.
- Currency: all shops price in JPY; the existing `utils.get_fx("jpy")` path is reused.
- Condition: most shops only sell NM English; a few (Toretoku, Cardrush) grade S/A/B/C/D — decide whether to filter to NM or surface grade in records.
- Cloudflare-gated shops have separate considerations for *bulk* indexing (see "Scrape-on-search vs daily bulk" below).
