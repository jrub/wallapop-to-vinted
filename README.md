# wallapop-to-vinted

Migrate your active Wallapop listings to Vinted.

![Python](https://img.shields.io/badge/python-3.10%2B-blue) ![License](https://img.shields.io/badge/license-MIT-green)

> Personal-use project. It relies on unofficial APIs and browser automation, which may violate Wallapop's or Vinted's Terms of Service — use it at your own risk. See [Legal notice & disclaimer](#legal-notice--disclaimer) for the full terms.

## How it works

Wallapop has no public API. Vinted has no public write API for individual accounts. This tool bridges them:

- **Extract** (`extract_wallapop.py`): calls Wallapop's internal REST API (reverse-engineered from browser traffic), resolves the numeric user ID from the `__NEXT_DATA__` blob on your profile page, and downloads every active listing + its images to `data/`.
- **Upload** (`upload_vinted.py`): drives a visible Chromium instance via [Patchright](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright) (a Playwright fork that patches bot-detection signals). Logs in, fills the `/items/new` form per listing, tries to publish; on validation errors falls back to saving as draft.

> Only `vinted.es` is supported. The DOM selectors (`"Subir"`, `"Publicar sin marca"`, `"Estado"`, …) are hardcoded in Spanish.

## Requirements

- Python 3.10+
- A Wallapop account with active listings
- A Vinted account in a supported country (`es`, `fr`, `de`, `it`, …)

## Installation

```bash
git clone https://github.com/<your-user>/wallapop-to-vinted.git
cd wallapop-to-vinted

python -m venv .venv
source .venv/bin/activate

pip install -r requirements.txt
patchright install chromium

cp .env.example .env
```

## Configuration

Edit `.env` with your credentials:

```env
WALLAPOP_USER_ID=your-slug-123456   # from es.wallapop.com/user/<slug>
VINTED_EMAIL=
VINTED_PASSWORD=
```

## Usage

**1. Extract from Wallapop:**

```bash
python extract_wallapop.py
```

Produces `data/downloaded_items.json` and downloads images to `data/images/`. Re-runs only fetch new listings. For any unknown `category_id`, a stub entry with `"vinted": null` is appended to `data/category_mapping.json` for you to map manually.

**2. Upload to Vinted:**

```bash
python upload_vinted.py                   # full run (headless)
python upload_vinted.py --visible         # visible browser — needed to solve a DataDome captcha manually
python upload_vinted.py --limit 1         # test one listing first
python upload_vinted.py --retry-drafts    # re-open existing drafts and try to publish them
python upload_vinted.py --no-learn        # freeze category_mapping.json (no auto-learning)
```

The browser runs headless by default. Vinted uses DataDome for bot protection, which can challenge any request — login, navigation, uploads — when it flags the behaviour as suspicious (suspicious IP, JS disabled, interactions deemed too fast, etc.). When the script detects the challenge page it aborts with a clear message: re-run with `--visible`, solve the slider once, and the refreshed session in `data/auth_state.json` lets subsequent runs go back to headless.

Failed items are logged; the run doesn't abort.

At the end of the run, the script prints a summary: drafts and their missing fields, newly auto-learned mappings, unresolved labels with the Vinted options seen, and categories still without a navigation path.

## Auto-learning category mappings

Vinted's upload form is dynamic: different categories ask for different attributes (phones need storage and SIM lock, books need ISBN, computers need RAM and OS, etc.). Hand-mapping every Wallapop attribute to every Vinted dropdown per category would be tedious, so the uploader does it on the fly.

The first time a listing from a new Wallapop category is uploaded:

1. The script scans the rendered Vinted form and reads each dropdown's label.
2. For each label, it tries to guess the matching key in the Wallapop item attributes — by direct name match, a small alias table (`Sistema operativo` → `operating_system`, `Talla` → `size`, …), or a fuzzy fallback.
3. Resolved mappings are written to `data/category_mapping.json` under the category's `attributes` block, so subsequent uploads reuse them directly.
4. Labels that couldn't be resolved are persisted with `from: null` and the list of options Vinted offered (`observed_options`), so you can finish the mapping by hand — either by setting `from` to the right Wallapop attribute or by adding a `value_map` for vocabulary mismatches.

The result: `data/category_mapping.json` grows with every run until it covers your inventory. Pass `--no-learn` to freeze it.

## Known limitations

- **Not every category works out of the box.** Vinted's form varies dramatically across categories (books need ISBN, phones need storage + SIM lock, etc.). When Wallapop doesn't provide the required value, the item stays as a draft for you to complete manually.
- **DataDome captchas.** Vinted's bot-protection can challenge *any* request — login, navigation, or upload — when it flags the behaviour as suspicious. The script detects the challenge page, aborts in headless mode, and tells you to re-run with `--visible` so you can solve the slider manually.
- **Manual publishing for some drafts.** Items saved as drafts need you to finish them in Vinted's UI (add the missing fields and click publish).
- **Unofficial APIs break.** Wallapop's endpoints and Vinted's DOM change without notice. When they do, things stop working until the code is updated.
- **Active listings only.** Sold, reserved, or Wallapop-draft items are ignored.
- **No deletion sync.** Removing an item from Wallapop will not remove it from Vinted.

## Data files

- `data/downloaded_items.json` — scraped from Wallapop, not versioned
- `data/migration.json` — upload state per item (`status`, `missing_fields`, `last_error`), not versioned
- `data/category_mapping.json` — Wallapop category → Vinted navigation path + attribute mappings, **versioned**
- `data/wallapop_categories.json` — full Wallapop category tree (reference only), versioned. [Generated with this script](https://gist.github.com/jrub/106958f23d850497c265e72ab19c3194).
- `data/auth_state.json` — saved browser session, not versioned
- `data/images/` — downloaded images, not versioned

## Legal notice & disclaimer

**This project is not affiliated with, endorsed by, or sponsored by Wallapop or Vinted.** All trademarks belong to their respective owners.

- **Intended use.** This tool is provided for personal, non-commercial use — specifically, migrating your own listings between your own accounts. Do not use it to scrape third parties' data, operate at scale, or bypass platform rate limits.
- **Terms of service.** Wallapop's and Vinted's Terms of Service may prohibit automated access, scraping, or the use of unofficial APIs. By running this software, **you alone are responsible** for reviewing those terms and accepting any consequences — including account warnings, suspension, or termination. The authors and contributors accept no responsibility for actions taken against your account.
- **No warranty.** The software is provided "AS IS", without warranty of any kind, express or implied. See the [LICENSE](LICENSE.md) file for the full disclaimer.
- **No liability.** In no event shall the authors or contributors be liable for any claim, damages, or other liability arising from the use of this software.
- **Data handling.** The tool runs entirely on your machine. Your credentials are read from `.env` and used only to authenticate against Vinted in your own browser session; nothing is sent anywhere else.
