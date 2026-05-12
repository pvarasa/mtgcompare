# Feature Gaps vs. Competitor MTG Tools

Comparison baseline: EchoMTG, Deckbox, MTGStocks (+Premium), Moxfield, Archidekt,
ManaBox, Delver Lens, Dragon Shield, TCGPriceAlert, MTGPrice, Quiet Speculation,
CardCastle.

mtgcompare's current positioning: cross-shop price comparison weighted toward
Japanese stores, inventory with cost-basis / P&L, MTGJSON daily price history
with charts, decklist pricing across shops with shipping awareness, optional
WorkOS multi-user.

The list below contains only the **important** and **nice-to-have** tiers,
in priority order within each tier. Low-priority items (proxy printing, deck
playtesting, tournament tracking, social comments, etc.) are omitted.

Last reconciled with shipped code at v1.6.9. The v1.6.x train was a
search-performance pass (basic-land stripping, per-shop wall-clock
cap with UI surfacing of timed-out shops, parser swap to selectolax,
Scryfall page streaming) rather than feature work — none of the items
below were touched by it.

## Already shipped (was on the original list)

- **Mobile-responsive UI.** Viewport meta, scrollable tables, sticky tabs,
  44px touch targets at ≤540px breakpoint, scoped hover. Shipped through
  v1.5.16 (commits `2471507`, `21f6444`).
- **Card preview on click (modal).** `.card-icon` buttons in search,
  inventory, market, and decklist views open a Scryfall-image modal,
  double-faced cards included (`mtgcompare/static/cardpreview.js`,
  `mtgcompare/templates/_macros.html`). The remaining piece — *inline
  thumbnails in list views* — is still open; tracked in Important #3
  below.
- **Per-card price-history chart.** SVG line chart with area fill on the
  Market page (`mtgcompare/templates/market.html` `buildChart()`). The
  remaining piece — *portfolio-wide value over time* — is still open;
  tracked in Important #7 below.

## Important — users actively expect these

1. **Price alerts across shops.** Threshold + drop alerts per card per
   condition, delivered via email (and ideally Telegram/Discord webhook).
   Single most-requested feature on every price-tracking tool (MTGStocks
   Premium, TCGPriceAlert, TCGAlert build their pitch around it). A
   cross-shop comparator without alerts is the biggest gap relative to the
   project's positioning.
2. **Wishlist / watchlist.** A "cards I want to buy" list separate from
   inventory, with current best-shop price and (combined with #1) alerts
   when any shop crosses a target. Deckbox, EchoMTG, MTGStocks all consider
   this core.
3. **Inline card thumbnails in list views.** Click-to-preview modal already
   ships; the remaining gap is showing art *inline* in search, inventory,
   and decklist tables so the eye can scan without clicking. All
   competitors render art inline; text-only rows feel dated.
4. **Advanced card-search filters.** Set, rarity, type, color/identity, mana
   cost, format legality, foil, language. Current search is name-only
   (`searchautocomplete.js`) — Deckbox, Moxfield, Scryfall-style filters
   are the baseline expectation. Foil and language are tracked on inventory
   rows but not as search-result filters.
5. **Format legality + format-aware filters.** Standard/Pioneer/Modern/
   Legacy/Vintage/Commander legality on each card and as a filter. Common
   in Moxfield, Archidekt, Dragon Shield. No legality data is fetched or
   cached today.
6. **Deck import from Moxfield / Archidekt URLs (and exports back).** Most
   users build decks there; pasting a URL beats copy-pasting text. Both
   expose deck JSON. Greatly broadens the audience for the decklist-pricing
   flow. Plain-text decklist parsing already exists (`web._parse_decklist`);
   URL importers do not.
7. **Set completion tracking.** "X / Y in [set], Z% complete" view for
   inventory. Standard in EchoMTG, Deckbox, Dragon Shield.
8. **Portfolio value over time (chart, not snapshot).** Today the dashboard
   shows a current P&L number, and the per-card chart on the Market page
   proves the rendering plumbing works. What's missing is summing inventory
   lots × historical prices into a portfolio-wide line chart over
   weeks/months. The daily price-history data and inventory acquisition
   dates needed for it are already present. EchoMTG and MTGStocks
   Portfolio do this.

## Nice to have — real value but not table-stakes

1. **Top movers / trending dashboard.** Biggest 24h / 7d / 30d gainers and
   losers across the universe (or scoped to the user's inventory +
   watchlist). MTGStocks "Movers", MTGPrice tracker, TCG Collector Tools
   all lean on this. Daily data is already ingested.
2. **Email digest (weekly).** Portfolio summary, watchlist movements,
   biggest movers in user's collection. EchoMTG sends these automatically;
   high engagement-per-effort ratio.
3. **Buylist / trade-in price tracking.** Where shops publish buylist prices
   (e.g. Hareruya), capture them and show buy/sell spread. Distinguishes
   finance-oriented tools (Quiet Speculation, MTGPrice).
4. **Multi-card overlay on the history chart.** Compare 2–5 cards on the
   same axes (especially useful for reprint analysis and spec comparisons).
5. **Mobile camera card scanning for bulk inventory entry.** Highest-lift
   item, but the most-loved feature on Delver Lens, ManaBox, Dragon Shield,
   MagicFrame, CardCastle. Even a "scan one at a time" v1 reduces the
   manual-entry friction that keeps casual collectors away.
6. **Public read-only inventory / tradelist share link.** EchoMTG and
   Deckbox lean on this for community/trade-finding. Cheap to ship given
   the existing per-user model.
7. **Tags / custom folders on inventory rows.** Users segment by deck, by
   binder, by "for sale". Common in Moxfield, Archidekt, EchoMTG.
8. **Bulk export in interoperable formats** (Deckbox CSV, Archidekt CSV,
   Moxfield CSV, MTGO/Arena .txt). CSV import already exists; the symmetric
   export prevents lock-in.
9. **Read-only public API** (price history for a card, current cross-shop
   prices). Power users and devs will build things on it; cheap PR. MTGStocks
   and Scryfall both benefit from this.
10. **Per-shop in-stock filtering and "consolidate cart" view for decklists.**
    Niche to the JP-shops focus — show which single shop can fulfill the
    largest fraction of a list to minimize shipments. No competitor does this
    well because no one else focuses on multi-JP-shop sourcing; potential
    differentiator rather than a parity feature.
11. **Currency / FX preferences per user.** USD ↔ JPY ↔ EUR display toggle,
    custom FX override. Today JPY↔USD is fetched and shown side-by-side
    (`web._get_fx`); EUR and a user-controlled toggle are missing.
12. **Language filter on shop search results.** JP shops list JP-language
    printings prominently; players who want only English (or only JP) want
    to filter at the source. Inventory rows track language already; shop
    search does not yet accept it as a filter.

## Recommended sequencing

With mobile polish and the click-to-preview modal already shipped, the next
two-to-three releases should focus on:

1. **Inline card thumbnails (Important #3).** Smallest unit of work, biggest
   visible-quality jump per LOC. Reuses the Scryfall fetch path the modal
   already uses; just needs an `image_uris.small` thumbnail column in the
   list templates and a lazy-load. Lands the "looks like a 2026 product"
   impression for free.
2. **Wishlist / watchlist (Important #2).** Schema is one new table keyed
   on `(user_id, card_name, set_code, finish, target_price)`. Reuses
   existing search and price-fetch paths. Prerequisite for alerts.
3. **Price alerts (Important #1).** Once watchlist exists, alerts are a
   thin layer: a daily job scans `market_prices` against watchlist target
   prices and posts to a notification channel. Email first (lowest
   integration cost — production already has SMTP-ready WorkOS email);
   Telegram/Discord webhook second.

After that, Important #8 (portfolio-value chart) is the next-highest payoff
because the data and the SVG renderer both already exist — it's mostly
aggregation SQL plus a new endpoint.

Defer Important #4–#7 (advanced filters, format legality, deck-URL import,
set completion) until at least one of the alerts/watchlist/portfolio-chart
features has shipped, so the project keeps drifting toward "best
cross-shop price tracker" rather than "yet another deck builder."
