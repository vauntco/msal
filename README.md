# 313 Car Loans — New Website

A modern, fast, conversion-focused website for **Matthew's Stop and Look Auto Sales**
(McQueen Auto, Inc.) — 8146 E 8 Mile Rd, Detroit, MI 48234 · 313-891-8000 —
currently at [313carloans.com](https://313carloans.com).

---

## What their current site runs on

The existing site is built by **[Dealer Car Search](https://www.dealercarsearch.com)** —
a website + back-office vendor for independent used-car dealers. You can tell from the
template URL structure (`/newandusedcars`, `/vdp/…` vehicle pages, `/bdp/…` blog pages,
`/creditapp`, `/locatorservice`, `/meetourstaff`), which is Dealer Car Search's standard
site skeleton.

That's also the "software that handles a whole bunch of their backend stuff":
Dealer Car Search isn't just the website — it's their **inventory manager and lead
system**. Dealers log in, add a car with photos once, and it feeds the website *and*
syndicates listings out to Facebook Marketplace, Craigslist, CarGurus, etc., and
collects leads/credit apps back in. Keep that in mind before fully replacing it:
the smart play is usually to **replace the front-end website** (this project) while
they keep using their existing back office for inventory + syndication — or move to
any other DMS later, since this site just reads a data file and can be fed by anything.

## What this project is

A **zero-dependency static website** — plain HTML/CSS/JS, no framework, no build
step, hostable anywhere for free (GitHub Pages, Netlify, Cloudflare Pages). All
vehicle data lives in one file: **`data/inventory.js`**. Change that file, and the
whole site updates.

```
313-car-loans/
├── index.html          Homepage — hero, quick search, featured cars, financing CTA
├── inventory.html      Full inventory with live filters (make/body/price/miles) + sort
├── vehicle.html        Vehicle detail page (?id=…) — gallery, specs, payment calculator
├── financing.html      Pre-approval + trade-in form (soft inquiry, no SSN collected)
├── about.html          About, hours, map, directions
├── css/styles.css      All styling (design tokens at the top for easy re-theming)
├── js/site.js          All behavior (rendering, filters, calculator)
├── data/inventory.js   ⭐ THE data file — every car on the site lives here
├── assets/             Placeholder images (+ downloaded photos after scraping)
└── scraper/scrape_inventory.py   Pulls real inventory from 313carloans.com
```

## Quick start

Just open `index.html` in a browser — it works from disk. For a proper local server:

```bash
python3 -m http.server 8080
# → http://localhost:8080
```

## Run it on Replit

Easiest way to work on this site with zero setup:

1. Go to [replit.com](https://replit.com) and log in (free account is fine).
2. Click **Create Repl**, then **Import from GitHub**.
3. Paste this repo URL: `https://github.com/CyberAllante/313-car-loans-website`
   (or use the one-click link: [replit.com/new/github/CyberAllante/313-car-loans-website](https://replit.com/new/github/CyberAllante/313-car-loans-website))
4. Hit **Run**. The included `.replit` file already sets the run command, so the site opens in the webview immediately.

Notes:
- The repo is public, so no GitHub account connection is required to import it.
- Importing creates your own copy on Replit. Edits there do not push back to this repo unless you connect GitHub and use the Git pane.
- The admin panel works on Replit too: open `/admin.html` in the webview URL.

## Pulling their real inventory

The scraper reads the live site's sitemap, visits every vehicle page, and rewrites
`data/inventory.js` with real cars, prices, mileage, VINs and photo URLs.
Run it from any normal machine with internet access:

```bash
cd scraper
pip install requests beautifulsoup4
python3 scrape_inventory.py --limit 3      # test on 3 cars first
python3 scrape_inventory.py                # full inventory, hotlinks their photo CDN
python3 scrape_inventory.py --download-images   # also saves photos into assets/photos/
```

Use `--download-images` for the real launch so the site doesn't depend on
Dealer Car Search's CDN staying up. Re-run whenever the lot changes, then push /
re-deploy. (It's the dealership scraping its own data — still, the script is
polite and rate-limited.)

**Ongoing updates without scraping:** `data/inventory.js` is human-editable.
Adding a car is copy-pasting a block and changing the fields. Down the road you
can generate it from any DMS export (Frazer, DealerCenter, Wayne Reaves and
Dealer Car Search can all export CSV inventory feeds).

## Managing inventory WITHOUT Dealer Car Search (admin.html)

`admin.html` is the dealership's own back office — the "super easy way to add
cars." Open it in any browser (it's in the site folder; not linked from the
public pages):

1. **+ Add a Car** → fill the form → drag photos in (auto-resized) → Save
2. Toggle **Featured** / **Sold**, edit or delete from the list
3. Click **Download inventory.js** → replace `data/inventory.js` → re-deploy

Work autosaves in the browser until downloaded. No accounts, no server, no
monthly fee. When you build your own automations later, the contract stays the
same: anything that writes `data/inventory.js` (a cron job, a DMS export
converter, a phone app) updates the whole site.

What Dealer Car Search still does that this doesn't (yet): syndication to
Facebook Marketplace / Craigslist / CarGurus, lead CRM, and hosted secure
credit-app processing. Keep those in mind before the dealership cancels — the
site is ready to plug into replacements whenever the automations exist.

## Deploying free on GitHub Pages

1. This repo already has the site at the root, so: Repo → Settings → Pages →
   deploy from branch `main`, folder `/ (root)`.
2. Point the `313carloans.com` domain (or a new one) at GitHub Pages when ready.

Netlify / Cloudflare Pages: drag-and-drop the folder — done.

## Wiring up the lead form

The pre-approval form on `financing.html` is pre-wired for
[FormRelay](https://www.useformrelay.com). To turn it on:

1. Get a free access key at [useformrelay.com](https://www.useformrelay.com)
   (or let Claude Code / Cursor create one over FormRelay's MCP).
2. Paste it into `FORMRELAY_ACCESS_KEY` at the top of the financing section in
   `js/site.js`.

That's it. Submissions get emailed, stored in the FormRelay inbox, and
spam-filtered (the form already includes the honeypot field). Until a key is
set, the form falls back to opening the visitor's email app (mailto) with the
details pre-filled.

**Important:** the form deliberately collects **no SSN or date of birth**. A full
credit application with SSN requires a secure, compliant backend (SSL + encryption
+ GLBA safeguards). Options: keep linking to their existing secure credit app, or
use a dealer-finance form service (700Credit QuickQualify, etc.). Don't collect
SSNs through a static-site form.

## Things to verify with the owner before launch

- [x] Hero photo: real vehicle photo from their site (`assets/hero-car.jpg`). Swap the file
      any time for a pro shot of the lot — same filename, no other change needed
- [x] Credit app: every "Apply For Credit" button links to their existing secure
      application at https://313carloans.com/creditapp (Dealer Car Search hosted)
- [ ] Business hours (currently placeholders: M–F 10–6, Sat 10–4, Sun closed)
- [x] Address confirmed: 8146 E 8 Mile Rd (M-102) at Van Dyke — matches their site and the street signs in their logo
- [ ] Real customer reviews to replace the sample ones on the homepage
- [ ] Lead email address (form currently targets `sales@313carloans.com` — likely wrong)
- [x] Logo: real transparent PNG in `assets/logo.png` (owner-provided); `assets/logo.svg` kept as a spare octagon mark
- [ ] Whether they're keeping Dealer Car Search for back-office (recommended at first)

## Re-theming in 30 seconds

All colors are CSS variables at the top of `css/styles.css`:

```css
--dark-800: #1a1c21;    /* header / dark sections */
--brand-600: #c1121f;   /* stop-sign red — CTAs + accents */
```

Change those two and the whole site follows. The real logo lives in `assets/logo.png`
(header, hero, footer); the favicon is a small octagon inline in each page's `<head>`.
