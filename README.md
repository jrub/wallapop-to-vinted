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
pip install -r requirements-dev.txt  # optional, only to run the test suite
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
python upload_vinted.py --limit 1         # one real upload attempt (skips already-done & unmapped first)
python upload_vinted.py --retry-drafts    # re-open existing drafts and try to publish them
python upload_vinted.py --no-learn        # freeze category_mapping.json (no auto-learning)
python upload_vinted.py --item <id>       # upload exactly one Wallapop item by its id (bypasses migration filter)
python upload_vinted.py --item            # interactive: pick one pending item from a numbered menu
```

The browser runs headless by default. Vinted uses DataDome for bot protection, which can challenge any request — login, navigation, uploads — when it flags the behaviour as suspicious (suspicious IP, JS disabled, interactions deemed too fast, etc.). When the script detects the challenge page it aborts with a clear message: re-run with `--visible`, solve the slider once, and the refreshed session in `data/auth_state.json` lets subsequent runs go back to headless.

Use `--item <id>` to retry an item that's already published or stuck as a draft (the migration filter normally skips those). With no value, `--item` lists every pending item with a number — type the number to pick one. Mutually exclusive with `--retry-drafts`.

Failed items are logged; the run doesn't abort.

At the end of the run, the script prints a summary: drafts and their missing fields, newly auto-learned mappings, unresolved labels with the Vinted options seen, and categories still without a navigation path.

## Pre-flight prompts

Before the browser opens, the uploader checks for two cases where Vinted needs information Wallapop never asked for. You answer once at the terminal, the answer is saved into `data/downloaded_items.json`, and re-runs reuse it without prompting.

**ISBN for books.** Vinted refuses to publish anything under "Libros" without an ISBN, but Wallapop treats ISBN as an optional free-text field that most sellers leave blank. For each book in the queue with no ISBN, you'll see the title and a prompt — type the ISBN to fill it, or press Enter to skip the item for this run *and all future runs* (a `skip_reason: "missing_isbn"` marker is persisted; clear it from `downloaded_items.json` to retry later). Filling the ISBN makes Vinted auto-populate Autor / Editorial / Idioma / Formato so you don't need to map those by hand.

**Leaf disambiguation for vague categories.** Wallapop lets sellers stop at intermediate nodes (e.g. "Componentes de PC", "Chaquetas") that Vinted splits across many leaves (RAM / GPU / motherboard, or 12 chaqueta sub-types). When the automatic resolver can't pick a leaf from the item's title and description, you'll see the candidates numbered — type the number to pick. Your pick is saved as `vinted_nav_override` on that item so re-runs use the same leaf without asking. Press Enter to skip; the item falls back to a Vinted draft for you to finish in the UI.

Both prompts are skipped under `--retry-drafts` (drafts already exist on Vinted and the data we'd need to prompt against is opaque at that stage).

## Auto-learning category mappings

Vinted's upload form is dynamic: different categories ask for different attributes (phones need storage and SIM lock, books need ISBN, computers need RAM and OS, etc.). Hand-mapping every Wallapop attribute to every Vinted dropdown per category would be tedious, so the uploader does it on the fly.

The first time a listing from a new Wallapop category is uploaded:

1. The script scans the rendered Vinted form and reads each dropdown's label.
2. For each label, it tries to guess the matching key in the Wallapop item attributes — by direct name match, a small alias table (`Sistema operativo` → `operating_system`, `Talla` → `size`, …), or a fuzzy fallback.
3. Resolved mappings are written to `data/category_mapping.json` under the category's `attributes` block, so subsequent uploads reuse them directly.
4. Labels that couldn't be resolved are persisted with `from: null` and the list of options Vinted offered (`observed_options`), so you can finish the mapping by hand — either by setting `from` to the right Wallapop attribute or by adding a `value_map` for vocabulary mismatches.

**Nav paths resolve per item.** If you leave a Wallapop category mapped to a partial Vinted path (one level short of a leaf), the uploader tries to pick the right leaf **using the item's own title + description**. For each sub-option it stems the main word (crude `es`/`s` plural strip) and checks whether it appears in the item text; if exactly one sub-option matches, it picks that leaf and continues. This matters because a single Wallapop category often maps to several Vinted leaves (e.g. "Dispositivos de red" → Routers / Repetidores / Módems) — the right leaf depends on the individual item, not on the category. When zero or multiple sub-options match, you're prompted to pick interactively at pre-flight (see [Pre-flight prompts](#pre-flight-prompts)). If you skip the prompt, the item falls back to draft on Vinted so you can finish it in the UI.

The result: `data/category_mapping.json` grows with every run until it covers your inventory. Pass `--no-learn` to freeze it.

## Known limitations

- **Not every category works out of the box.** Vinted's form varies dramatically across categories (phones need storage + SIM lock, etc.). When Wallapop doesn't provide the required value, the item stays as a draft for you to complete manually. Books and ambiguous-leaf categories are handled by the [pre-flight prompts](#pre-flight-prompts).
- **Package size: Vinted's default ("Mediano") is used as-is.** Wallapop has no equivalent attribute, so the uploader doesn't touch the package-size selector — Vinted's pre-selected "Mediano" applies to every listing. If a specific item needs Pequeño, Grande, or XGrande, edit the listing on Vinted after publish.
- **DataDome captchas.** Vinted's bot-protection can challenge *any* request — login, navigation, or upload — when it flags the behaviour as suspicious. The script detects the challenge page, aborts in headless mode, and tells you to re-run with `--visible` so you can solve the slider manually.
- **Manual publishing for some drafts.** Items saved as drafts need you to finish them in Vinted's UI (add the missing fields and click publish).
- **Unofficial APIs break.** Wallapop's endpoints and Vinted's DOM change without notice. When they do, things stop working until the code is updated.
- **Active listings only.** Sold, reserved, or Wallapop-draft items are ignored.
- **No deletion sync.** Removing an item from Wallapop will not remove it from Vinted.

## Data files

- `data/downloaded_items.json` — scraped from Wallapop, not versioned. The pre-flight prompts also write back to this file: `attributes.isbn` (gathered ISBN), `skip_reason` (item you opted out of, e.g. `"missing_isbn"`), and `vinted_nav_override` (leaf you picked manually). Edit the file by hand to undo any of these decisions.
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
