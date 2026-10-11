# Pantri Recipe Importer

A small, offline importer for recipe information you are entitled to use. It turns local JSON, schema.org Recipe JSON-LD and CSV exports into normalized JSON and linked CSV tables. It never visits a website, follows JSON-LD contexts, calls private APIs, downloads images, or changes Pantri production.

**Status:** implementation and synthetic tests, not a full recipe catalogue. No collected third-party recipe records are included in this public repository or code ZIP. Workbooks and gathered exports are delivered separately and must remain private unless permission explicitly covers publication.

## Run it

Python 3.10 or newer is required. No installation, packages, API keys or network are needed.

```sh
python3 -m unittest discover -s tests -v
python3 importer.py examples/synthetic.json --output demo-output --usage synthetic --rights-reference "Self-authored synthetic demonstration"
```

Every example is clearly synthetic. The example URL uses the reserved `.test` domain and is never fetched. Use a new output directory for each import. The parent directory must already exist.

For a supplied export with appropriate permission:

```sh
python3 importer.py inputs/authorized-export.json --output outputs/batch-001 --usage authorized --rights-reference "Supplier export licence reference, date and permitted use"
```

Create `inputs` and `outputs` first and keep them private. Do not put credentials, confidential contract text or personal data in the rights reference. The reference is recorded in exported files; it is an audit note, not a permission grant or an automated legal check.

## Your requested fields

| Requested field | Normalized output |
|---|---|
| Recipe Title | `title` |
| Prep time / Total Time | `timings.prep_minutes` / `timings.total_minutes` |
| Difficulty level | `difficulty` |
| Ingredients | ordered `ingredients` and `ingredients.csv` |
| Ingredient details | source name, conservative canonical name, original ingredient text, quantity, quantity text, unit and preparation note |
| Description | `description`, gated as described below |
| Method + Steps | `method_summary`, ordered `steps`, and `steps.csv`, gated below |
| Dietary tags | `diets`, retained as source labels |
| Overall review rating score | `aggregate_rating.rating_value`, with observed scale and counts |

Source attribution, capture status, parser version, validation warnings, stable IDs and content hashes accompany the data. Unknown values remain null. Retailer mapping is intentionally omitted. Canonical names are trimmed, whitespace-collapsed, lowercased source names, with qualifiers preserved. No Pantri master ingredient list has been loaded or matched. `canonical_match_status` flags ambiguous forms (including rice, stock, bunches and blocks) and missing quantities for review. The importer does not infer nutrition, allergens, missing cook times, unit conversions or ingredient equivalence.

## Description and steps: explicit text permission

By default, descriptions, methods, steps, images, authors, written reviews and reviewer identities are omitted. The recipe has `content_status: "not_imported_requires_authorized_text"`; description and method summary are null, and steps are empty. These are intentionally unavailable, not verified empty source fields.

For user-owned or specifically licensed supplied prose, both a flag and a separate nonsecret text-rights reference are required, with `--usage authorized`:

```sh
python3 importer.py inputs/owned-recipes.json --output outputs/owned-001 --usage authorized --rights-reference "Own recipe collection" --include-authorized-text --text-rights-reference "I own this supplied recipe prose and authorize its use"
```

The option imports supplied `description`, `method_summary` and `steps` (also schema.org `recipeInstructions`). It does not retrieve anything. The source order is retained. A single instruction string stays one step; the importer does not invent step boundaries. JSON-LD HowToSection groups are flattened with section labels. Images, written reviews and identities are always excluded.

Do not use the text option as a workaround for missing permission. Changing wording does not establish permission to collect or reuse a source. Before any full-site collection, obtain an authorized export or suitable source permission. The optional public-page collector described below must be invoked separately; the importer itself never fetches URLs.

## Input formats

1. JSON: one recipe object, an array, or an object with a `recipes` array. See `examples/synthetic.json`.
2. JSON-LD: a Recipe object, Recipe arrays, or an `@graph`. Unrelated typed nodes are ignored. Local JSON-LD only; HTML is not accepted and external contexts are never resolved.
3. UTF-8 CSV: one recipe per row. Use `canonical_url,title` plus optional flat fields. Complex columns (`ingredients`, `categories`, `diets`, `steps`, `timings`, `aggregate_rating`, `nutrition`, `provenance`) contain JSON values, correctly CSV-quoted. See `examples/synthetic.csv`.

Accepted aliases include `source_url`/`url`, `name`, `recipeCategory`, `suitableForDiet`, `recipeYield`, ISO-8601 `prepTime`/`cookTime`/`totalTime`, `recipeIngredient`, `aggregateRating`, and the factual sample's `required_quantity`, `required_unit`, `preparation_note`, `source_ingredient_text` fields. Any supplied retailer pack mappings are ignored. Only documented allowlisted fields are retained. Existing normalized JSON can be supplied again, but CSV index exports are for viewing and joining, not a lossless round-trip format.

Free-text ingredients remain verbatim factual ingredient text with `text_only_not_inferred`; quantities are not guessed. `quantity` is numeric only when the supplied structured quantity is explicitly numeric; explicit decimal and fractional quantities are normalized arithmetically without unit conversion. Ranges and composite/container quantities remain in the original text for review. Supply structured ingredient objects when you have authoritative parsed values. Source `recipeYield: "4 servings"` remains yield text, rather than guessed numeric servings. Numeric `servings: 4` is retained directly.

## Output files

- `catalogue.json`: full normalized data; see `schema.json`.
- `recipes.csv`: one searchable summary row per recipe, including requested description/method fields and content status.
- `ingredients.csv`: linked ordered ingredients, source/canonical names, canonical-match status and factual cooking amounts.
- `steps.csv`: linked ordered authorized steps; header only when prose is unavailable.
- `nutrition.csv`: only supplied recognized observed nutrient fields, with their original units/value text. No computed totals or invented basis.
- `errors.json`: rejected file/row codes and duplicate decisions, without copying rejected source text.
- `manifest.json`: completion marker, input/output hashes, counts, parser version and rights references.

CSV is UTF-8 with a BOM for Excel. Formula-like string cells are prefixed with an apostrophe. JSON preserves original strings. Keep that prefix when opening or transforming CSV in spreadsheet software; stripping it removes the protection. Canonical URLs are data only and are never fetched by the offline importer.

## IDs, deduplication and provenance

Canonical URLs lowercase scheme/host, drop fragments and a trailing slash, remove only `utm_*`, `gclid` and `fbclid`, and sort remaining query pairs. Meaningful query parameters remain significant. URL credentials and non-HTTP(S) schemes are rejected. There is no redirect discovery or cross-domain equivalence guessing.

`recipe_id = "cp_" + sha256(canonical_url)[:16]`. The `cp_` namespace is retained for compatibility with the companion workbook and does not limit source websites. `ingredient_id = recipe_id + "_i" + ordinal` and `step_id = recipe_id + "_s" + ordinal`, padded to at least two digits. Ingredient/step IDs are stable for the same source order; inserting or reordering rows changes subsequent IDs.

Duplicates are grouped by canonical URL. The row with more ingredient entries wins, followed by lexicographically smallest normalized-fact hash and source hash. Conflicts are reported, never silently field-merged. This deterministic preference is not a correctness assessment; review competing sources. `dedup_hash` hashes normalized facts, excluding import timestamp and provenance. It changes when facts or authorized prose change. Cryptographic IDs are not a guarantee against theoretical collisions.

Capture dates refer to the source observation, not this import. Missing exact capture timestamps stay null. `imported_at` is separate. Dietary labels are source claims, not allergen or medical guarantees. Recipe instructions and reviews are not needed to retain aggregate rating numbers.

## Safe repeat runs

Re-run the identical command with `--resume` to validate all input hashes, parsing options, parser version and every output artifact hash, then return without rewriting anything:

```sh
python3 importer.py examples/synthetic.json --output demo-output --usage synthetic --rights-reference "Self-authored synthetic demonstration" --resume
```

This is verified completed-batch resumption, not an incremental database or an interrupted-run checkpoint. Changed inputs need a new output directory. An interrupted run without a complete manifest is refused; retain it for diagnosis and rerun into a new directory. No unknown directory or file is overwritten or deleted.

Files are written to temporary files, flushed and published with exclusive atomic hard links; the manifest is published last. Consumers must wait for and verify the completed manifest. Directory-wide atomic visibility and guaranteed recovery after hardware/filesystem failure are not claimed. Use a local filesystem supporting hard links (ordinary Linux/macOS/NTFS); unsupported filesystems fail safely. Do not run in a directory writable by untrusted concurrent processes.

Default ceilings: 100 input files, 20 MiB each / 100 MiB total, 20,000 candidate recipe rows, 500 ingredients per recipe, 300 steps, 30 JSON nesting levels, and 150 MiB output. An earlier normalized-data cap bounds memory before serialization. Oversized batches should be split. These are guardrails, not a tested promise of every maximum combined.

Exit codes: `0` completed or verified resume; `2` completed with file/record rejections (inspect `errors.json`); `1` blocked or unsafe operation. Warnings, unknown source fields and duplicate exclusions do not turn a completed batch into a fatal error.

## Development and release checks

```sh
python3 -m py_compile importer.py
python3 -m unittest discover -s tests -v
```

Tests use only authored synthetic data and temporary directories. The public repository contains code, documentation, tests, schema and synthetic examples. Collected source facts, workbook files, licensed exports, generated catalogues, credentials and private licence documents should never be committed. `.gitignore` helps, but review staged files before every commit.

This tool prepares data for review. It is not a Pantri production integration, a master-ingredient matching engine, a legal-permission checker or a complete catalogue.


## Optional permission-based public-page collector

`collector.py` is a separate, opt-in network tool for the public Cherrypick recipe pages. Use it only when permission covers automated collection and storage/reuse of recipe descriptions and methods. The rights references are audit notes, not legal verification. It does not publish data or import into Pantri.

```sh
python3 collector.py --output private/collection --limit 25 --rights-reference "Permission reference covering automated recipe collection" --text-rights-reference "Permission reference covering descriptions and ordered methods"
```

The `private` parent directory must exist. First validate the 25-recipe pilot and its snapshot before expanding the same saved queue:

```sh
python3 collector.py --output private/collection --limit 10000 --resume --rights-reference "Permission reference covering automated recipe collection" --text-rights-reference "Permission reference covering descriptions and ordered methods"
```

The limit is a ceiling, not a claim that that many recipes exist. The collector discovers a public sitemap queue, accepts only `/recipes/{single-slug}` pages, and records discovered/selected/accepted counts. It reads robots.txt, caches the response for at most 10 minutes keyed to origin and user-agent, refreshes expired or mismatched entries, fails closed on refresh errors, honors disallows and slower crawl rates, waits at least two seconds between requests, and uses at most two retries with backoff/Retry-After. It stays on the approved HTTPS origin, performs no login or private-API requests, downloads no photos and makes no proxy/CAPTCHA/security-warning bypass. It stops on access barriers, malformed recipe structure or missing ingredients/steps. A missing public page (HTTP 404) is reported and collection can continue.

The source adapter reads Recipe JSON-LD, the recipe's own header badges and the visible aggregate rating summary. Global navigation tags are excluded. This provider's JSON-LD `reviewCount` denotes all ratings; the adapter maps that to rating count and uses the visible summary for the separate written-review count. Source cook time is retained when present; it is never inferred by subtraction.

Conservative ingredient parsing recognizes explicit amounts and common units, preserving every original string and preparation note. Explicit Unicode fractions are handled, but unit conversions and pack multiplication are not performed. Parsed ingredient rows are marked `rule_parsed_reviewable`; local canonical labels are not authoritative master-list matches. Source dietary labels remain unverified even where ingredient text appears inconsistent with them.

Each successful recipe is saved privately with a content hash. Collector `--resume` verifies cached records and continues the saved queue without refetching successful pages; it can increase the limit after a reviewed pilot. This collector resume differs from the importer's completed-batch-only `--resume`. The same rights references and collector version are required. Output snapshots contain chunked authorized JSON exports, collection errors, a collection manifest and normalized JSON/CSV. An interrupted collection can retain safe successful records even before its final snapshot exists. Changed parsing rules require review of existing cached records before reuse.

For repeated small batches, add `--checkpoint-only` to retain all per-recipe records and hashes while writing only a compact completion summary. Omit that flag at meaningful milestones or the end to produce the full normalized JSON/CSV snapshot. This avoids repeatedly copying the whole growing catalogue after every tiny batch. It does not change request spacing or concurrency.

Never commit the private collection directory, real page fixtures, workbook data or exported recipe prose. Public code and synthetic tests are the only intended repository contents.
