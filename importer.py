#!/usr/bin/env python3
"""Offline, allowlisted recipe-fact import. Python 3.10+; no dependencies/network."""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import os
import re
import sys
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from fractions import Fraction
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

VERSION = "1.0.0"
SCHEMA_VERSION = "1.0"
MAX_FILE = 20 * 1024 * 1024
MAX_TOTAL = 100 * 1024 * 1024
MAX_OUTPUT = 150 * 1024 * 1024
MAX_RECORDS = 20000
MAX_INGREDIENTS = 500
MAX_TEXT = 8192
MAX_DEPTH = 30
POLICY_OMISSIONS = ["instructions", "descriptions", "review_text", "reviewer_identity", "images", "authors"]
FORBIDDEN_KEYS = {"recipeInstructions", "instructions", "directions", "method", "method_summary", "steps", "description", "review", "reviews", "review_text", "image", "images", "author"}
OUTPUT_NAMES = {"catalogue.json", "recipes.csv", "ingredients.csv", "steps.csv", "nutrition.csv", "errors.json"}


class ImportProblem(ValueError):
    pass


def compact(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(value if isinstance(value, bytes) else compact(value).encode("utf-8")).hexdigest()


def first(data, *keys):
    for key in keys:
        value = data.get(key)
        if value is not None and value != "":
            return value
    return None


def txt(value, limit=MAX_TEXT):
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ImportProblem("expected_scalar_text")
    value = str(value)
    if len(value) > limit or "\x00" in value:
        raise ImportProblem("text_limit_or_nul")
    return value


def number(value, *, integer=False, positive=False):
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ImportProblem("invalid_number")
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError):
        raise ImportProblem("invalid_number") from None
    if not math.isfinite(result) or result < 0 or (positive and result <= 0):
        raise ImportProblem("number_out_of_range")
    if integer and not result.is_integer():
        raise ImportProblem("expected_integer")
    return int(result) if result.is_integer() else result


def as_list(value):
    if value is None or value == "":
        return []
    return value if isinstance(value, list) else [value]


def labels(value):
    values = as_list(value)
    if len(values) > 100:
        raise ImportProblem("too_many_labels")
    return sorted({txt(v, 250) for v in values if v is not None and v != ""})


def mapping(value):
    if value is None or value == "":
        return {}
    if not isinstance(value, dict):
        raise ImportProblem("expected_object")
    return value


def canonical_url(value):
    value = txt(value, 2048)
    if not value or any(ord(c) <= 32 for c in value):
        raise ImportProblem("invalid_canonical_url")
    try:
        u = urlsplit(value)
        if u.scheme.lower() not in {"http", "https"} or not u.hostname or u.username or u.password:
            raise ValueError()
        host = u.hostname.encode("idna").decode("ascii").lower()
        if ":" in host:
            host = "[" + host + "]"
        port = u.port
        if port and not ((u.scheme.lower() == "http" and port == 80) or (u.scheme.lower() == "https" and port == 443)):
            host += ":" + str(port)
        params = [(k, v) for k, v in parse_qsl(u.query, keep_blank_values=True, max_num_fields=100)
                  if not k.lower().startswith("utm_") and k.lower() not in {"gclid", "fbclid"}]
        return urlunsplit((u.scheme.lower(), host, u.path.rstrip("/") or "/", urlencode(sorted(params)), ""))
    except (ValueError, UnicodeError):
        raise ImportProblem("invalid_canonical_url") from None


def minutes(value):
    if value is None or value == "":
        return None
    if isinstance(value, str) and value.startswith("P"):
        m = re.fullmatch(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?)?", value)
        if not m or not any(m.groups()):
            raise ImportProblem("invalid_iso_duration")
        d, h, minute, sec = (float(n or 0) for n in m.groups())
        return number(d * 1440 + h * 60 + minute + sec / 60)
    return number(value)


def iso_date(value, timestamp=False):
    value = txt(value, 100)
    if value is None:
        return None
    try:
        if timestamp:
            d = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if d.tzinfo is None:
                raise ValueError()
            return d.isoformat()
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise ImportProblem("invalid_capture_date") from None


def inspect_bounds(value, depth=0):
    if depth > MAX_DEPTH:
        raise ImportProblem("json_depth_limit")
    if isinstance(value, str) and len(value) > 250000:
        raise ImportProblem("input_string_limit")
    if isinstance(value, (dict, list)):
        if len(value) > MAX_RECORDS:
            raise ImportProblem("collection_limit")
        for v in (value.values() if isinstance(value, dict) else value):
            inspect_bounds(v, depth + 1)


def no_duplicate_keys(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ImportProblem("duplicate_json_key")
        obj[key] = value
    return obj


def parse_json(value):
    try:
        parsed = json.loads(value, object_pairs_hook=no_duplicate_keys,
                            parse_constant=lambda _: (_ for _ in ()).throw(ImportProblem("nonfinite_json")))
        inspect_bounds(parsed)
        return parsed
    except (json.JSONDecodeError, RecursionError, UnicodeError):
        raise ImportProblem("invalid_json") from None


def recipe_nodes(doc):
    if isinstance(doc, list):
        result = []
        for value in doc:
            result.extend(recipe_nodes(value))
        return result
    if not isinstance(doc, dict):
        raise ImportProblem("expected_recipe_object")
    if "recipes" in doc:
        if not isinstance(doc["recipes"], list):
            raise ImportProblem("expected_recipes_array")
        return recipe_nodes(doc["recipes"])
    if "@graph" in doc:
        return recipe_nodes(doc["@graph"])
    types = as_list(doc.get("@type"))
    if types and not any(str(t).split("/")[-1] == "Recipe" for t in types):
        return []
    return [doc]


def read_bytes_limited(path):
    if path.is_symlink() or not path.is_file():
        raise ImportProblem("input_not_regular_file")
    if path.stat().st_size > MAX_FILE:
        raise ImportProblem("input_file_size_limit")
    with path.open("rb") as handle:
        raw = handle.read(MAX_FILE + 1)
    if len(raw) > MAX_FILE:
        raise ImportProblem("input_file_size_limit")
    return raw


def read_input(path, raw=None):
    if raw is None:
        raw = read_bytes_limited(path)
    try:
        content = raw.decode("utf-8-sig")
    except UnicodeError:
        raise ImportProblem("input_not_utf8") from None
    if path.suffix.lower() == ".csv":
        csv.field_size_limit(250000)
        try:
            reader = csv.DictReader(io.StringIO(content), strict=True)
            fields = reader.fieldnames or []
            if not fields or len(fields) != len(set(fields)):
                raise ImportProblem("missing_or_duplicate_csv_headers")
            records = []
            nested = {"categories", "diets", "ingredients", "steps", "nutrition", "aggregate_rating", "timings", "provenance"}
            for row in reader:
                if None in row or any(v is None for v in row.values()):
                    raise ImportProblem("csv_column_count_mismatch")
                for key in nested & row.keys():
                    row[key] = parse_json(row[key]) if row[key].strip() else None
                records.append(row)
                if len(records) > MAX_RECORDS:
                    raise ImportProblem("record_limit")
        except csv.Error:
            raise ImportProblem("invalid_csv") from None
    elif path.suffix.lower() in {".json", ".jsonld"}:
        records = recipe_nodes(parse_json(content))
    else:
        raise ImportProblem("unsupported_input_extension")
    if not records:
        raise ImportProblem("no_recipe_records")
    if len(records) > MAX_RECORDS:
        raise ImportProblem("record_limit")
    return raw, records


UNICODE_FRACTIONS = {"¼":0.25,"½":0.5,"¾":0.75,"⅐":1/7,"⅑":1/9,"⅒":0.1,"⅓":1/3,"⅔":2/3,"⅕":0.2,"⅖":0.4,"⅗":0.6,"⅘":0.8,"⅙":1/6,"⅚":5/6,"⅛":0.125,"⅜":0.375,"⅝":0.625,"⅞":0.875}


def explicit_quantity(value):
    if not value:
        return None
    value = value.strip()
    if re.fullmatch(r"\d+(?:\.\d+)?", value):
        return number(value)
    fraction = re.fullmatch(r"(?:(\d+)\s*)?([" + "".join(UNICODE_FRACTIONS) + r"])", value)
    if fraction:
        return number(int(fraction[1] or 0) + UNICODE_FRACTIONS[fraction[2]])
    fraction = re.fullmatch(r"(?:(\d+)\s+)?(\d+)\s*/\s*(\d+)", value)
    if fraction and int(fraction[3]):
        return number(int(fraction[1] or 0) + float(Fraction(int(fraction[2]),int(fraction[3]))))
    return None


def ingredient(data, recipe_id, position):
    if isinstance(data, str):
        data = {"original_text": data}
    data = mapping(data)
    original = txt(first(data, "original_text", "source_ingredient_text", "text"))
    name = txt(first(data, "source_name", "name", "ingredient_name"), 1000)
    if not original and not name:
        raise ImportProblem("ingredient_missing_text")
    quantity = first(data, "quantity_text", "required_quantity", "quantity")
    quantity_text = txt(quantity, 100)
    quantity_number = explicit_quantity(quantity_text)
    unit = txt(first(data, "unit", "required_unit"), 100)
    canonical = " ".join(name.split()).lower() if name else None
    needs_review = not name or quantity_number is None or data.get("canonical_match_status") == "needs_form_review" or any(term in (canonical or "") for term in ("rice", "stock")) or any(term in (unit or "").lower() for term in ("bunch", "block"))
    return {"ingredient_id": recipe_id + "_i" + str(position).zfill(2), "position": position,
            "original_text": original, "source_name": name, "canonical_name": canonical,
            "canonical_match_status": "needs_form_review" if needs_review else "local_label_only_master_not_loaded",
            "quantity": quantity_number, "quantity_text": quantity_text, "unit": unit,
            "preparation": txt(first(data, "preparation", "preparation_note"), 1000),
            "parse_status": "rule_parsed_reviewable" if data.get("parse_status") == "rule_parsed_reviewable" else ("source_structured" if name else "text_only_not_inferred")}



NUTRIENTS = {"calories", "carbohydrateContent", "proteinContent", "fatContent", "saturatedFatContent",
             "unsaturatedFatContent", "transFatContent", "fiberContent", "sugarContent", "sodiumContent",
             "cholesterolContent", "servingSize", "energy_kj", "energy_kcal", "carbohydrate_g", "protein_g",
             "fat_g", "saturated_fat_g", "fibre_g", "sugar_g", "sodium_mg", "salt_g", "basis"}


def normalize_steps(value, recipe_id):
    result = []

    def walk(items, section=None, depth=0):
        if depth > 10:
            raise ImportProblem("steps_section_depth_limit")
        for item in as_list(items):
            if len(result) >= 300:
                raise ImportProblem("step_count_limit")
            if isinstance(item, str):
                item = {"text": item}
            item = mapping(item)
            if "itemListElement" in item:
                walk(item["itemListElement"], txt(item.get("name"), 1000) or section, depth + 1)
                continue
            text = txt(first(item, "text", "instruction"), 16000)
            name = txt(first(item, "name", "title"), 1000)
            if not text and not name:
                raise ImportProblem("step_missing_text")
            position = len(result) + 1
            result.append({"step_id": recipe_id + "_s" + str(position).zfill(2), "position": position,
                           "section": txt(item.get("section"), 1000) or section, "name": name, "text": text})
    walk(value)
    return result


def normalize(data, source, index, imported_at, usage, rights_reference, include_authorized_text=False, text_rights_reference=None):
    data = mapping(data)
    url = first(data, "canonical_url", "source_url", "url")
    if isinstance(url, dict):
        url = url.get("@id")
    if not url:
        page = data.get("mainEntityOfPage")
        url = page.get("@id") if isinstance(page, dict) else page
    if not url:
        url = data.get("@id")
    url = canonical_url(url)
    title = txt(first(data, "title", "name"), 1000)
    if not title or not title.strip():
        raise ImportProblem("missing_title")
    rid = "cp_" + hashlib.sha256(url.encode()).hexdigest()[:16]
    warnings = []
    items = first(data, "ingredients", "recipeIngredient") or []
    if not isinstance(items, list) or len(items) > MAX_INGREDIENTS:
        raise ImportProblem("ingredient_array_limit_or_type")
    ingredients = [ingredient(item, rid, position) for position, item in enumerate(items, 1)]
    times = mapping(data.get("timings"))
    timings = {k: minutes(first(times, k) if first(times, k) is not None else first(data, k, alias))
               for k, alias in [("prep_minutes", "prepTime"), ("cook_minutes", "cookTime"), ("total_minutes", "totalTime")]}
    if timings["cook_minutes"] is None:
        warnings.append("cook_time_not_observed_no_subtraction_inferred")
    if timings["total_minutes"] is not None and any(t is not None and t > timings["total_minutes"] for k, t in timings.items() if k != "total_minutes"):
        warnings.append("component_time_exceeds_total_review_source")
    serving_raw = first(data, "servings", "recipeYield")
    servings = None
    if isinstance(serving_raw, (str, int, float)) and re.fullmatch(r"\d+(?:\.\d+)?", str(serving_raw)):
        servings = number(serving_raw, positive=True)
    elif serving_raw is not None:
        warnings.append("servings_unparsed_preserved_as_yield_text")
    nutrition = mapping(data.get("nutrition"))
    nutrition = {k: txt(v, 300) for k, v in nutrition.items() if k in NUTRIENTS and v is not None}
    if not nutrition:
        warnings.append("nutrition_not_observed_no_inference")
    rating = mapping(first(data, "aggregate_rating", "aggregateRating"))
    rating = {k: number(first(rating, k, alias) if first(rating, k, alias) is not None else first(data, flat), integer=k in {"rating_count", "review_count"})
              for k, alias, flat in [("rating_value", "ratingValue", "rating_value"), ("best_rating", "bestRating", "rating_scale_max"),
                                     ("worst_rating", "worstRating", "rating_scale_min"), ("rating_count", "ratingCount", "rating_count"),
                                     ("review_count", "reviewCount", "written_review_count")]}
    if rating["rating_value"] is not None:
        if rating["best_rating"] is not None and rating["rating_value"] > rating["best_rating"]:
            raise ImportProblem("rating_above_scale")
        if rating["worst_rating"] is not None and rating["rating_value"] < rating["worst_rating"]:
            raise ImportProblem("rating_below_scale")
    prov = mapping(data.get("provenance"))
    captured = iso_date(first(prov, "captured_at") or data.get("captured_at"), timestamp=True)
    observed = iso_date(first(prov, "observed_on") or data.get("observed_on"))
    provenance = {"source_url": url, "source_file": source["name"], "source_file_sha256": source["sha256"],
                  "source_page_sha256": txt(prov.get("source_page_sha256"), 64),
                  "source_record_index": index, "captured_at": captured, "observed_on": observed,
                  "capture_time_status": "source_timestamp" if captured else "exact_timestamp_unknown",
                  "capture_method": txt(first(prov, "capture_method") or data.get("capture_method"), 1000),
                  "source_freshness_note": txt(first(prov, "source_freshness_note") or data.get("source_freshness_note"), 1000),
                  "permission_status": txt(first(prov, "permission_status") or data.get("permission_status"), 1000),
                  "imported_at": imported_at, "parser_version": VERSION, "usage": usage,
                  "rights_reference": rights_reference, "field_provenance": []}
    for p in as_list(data.get("field_provenance") or prov.get("field_provenance"))[:100]:
        p = mapping(p)
        provenance["field_provenance"].append({"fields": labels(p.get("fields")),
            "source_url": canonical_url(p["source_url"]) if p.get("source_url") else url,
            "source_section": txt(p.get("source_section"), 1000), "transformation": txt(p.get("transformation"), 1000)})
    if not captured:
        warnings.append("exact_source_capture_time_unknown")
    if not ingredients:
        warnings.append("ingredients_not_observed")
    if any(i["parse_status"] == "text_only_not_inferred" for i in ingredients):
        warnings.append("ingredient_quantities_not_inferred_from_free_text")
    diets = labels(first(data, "diets", "local_diet_labels", "suitableForDiet"))
    if diets:
        warnings.append("diet_labels_source_reported_not_allergen_guarantees")
    description, method_summary, steps = None, None, []
    if include_authorized_text:
        if usage != "authorized" or not text_rights_reference or not str(text_rights_reference).strip():
            raise ImportProblem("authorized_text_requires_authorized_usage_and_text_rights_reference")
        description = txt(data.get("description"), 32000)
        method_summary = txt(first(data, "method_summary", "method"), 32000)
        steps = normalize_steps(first(data, "steps", "recipeInstructions", "instructions", "directions"), rid)
        content_status = "authorized_text_imported" if description or method_summary or steps else "authorized_text_requested_but_absent"
    else:
        content_status = "not_imported_requires_authorized_text"
    provenance["text_rights_reference"] = txt(text_rights_reference, 1000) if include_authorized_text else None
    facts = {"recipe_id": rid, "canonical_url": url, "source_id": txt(first(data, "source_id", "id", "@id"), 2048),
             "title": title, "categories": labels(first(data, "categories", "local_recipe_tags", "recipeCategory")),
             "diets": diets, "servings": servings, "yield_text": txt(serving_raw, 200), "timings": timings,
             "difficulty": txt(data.get("difficulty"), 100), "ingredients": ingredients,
             "nutrition": nutrition or None,
             "aggregate_rating": rating if any(v is not None for v in rating.values()) else None,
             "allergens": None, "allergen_status": "not_inferred_or_validated",
             "description": description, "method_summary": method_summary, "steps": steps, "content_status": content_status}
    facts["dedup_hash"] = digest(facts)
    facts["provenance"] = provenance
    allowed_text_keys = {"description", "method", "method_summary", "steps", "recipeInstructions", "instructions", "directions"} if include_authorized_text else set()
    facts["omitted_fields"] = {"by_policy": [k for k in POLICY_OMISSIONS if not (include_authorized_text and k in {"instructions", "descriptions"})],
                               "input_keys_discarded_by_policy": sorted((FORBIDDEN_KEYS - allowed_text_keys) & data.keys())}
    facts["validation"] = {"status": "review_required" if warnings else "valid", "warnings": warnings,
                           "source_notes": [txt(s, 2000) for s in as_list(data.get("validation_notes"))[:100]]}
    return facts


def csv_safe(value):
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        value = compact(value)
    if isinstance(value, str) and (value.lstrip().startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n"))):
        return "'" + value
    return value


def csv_bytes(columns, rows):
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, columns, extrasaction="raise", lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: csv_safe(v) for k, v in row.items()})
    return stream.getvalue().encode("utf-8-sig")


def output_payloads(recipes, errors, usage, rights_reference):
    recipe_rows, ingredient_rows, step_rows, nutrition_rows = [], [], [], []
    for r in recipes:
        recipe_rows.append({"recipe_id": r["recipe_id"], "canonical_url": r["canonical_url"], "title": r["title"],
            "categories": r["categories"], "diets": r["diets"], "servings": r["servings"], **r["timings"],
            "difficulty": r["difficulty"], "ingredient_count": len(r["ingredients"]),
            "rating_value": (r["aggregate_rating"] or {}).get("rating_value"),
            "best_rating": (r["aggregate_rating"] or {}).get("best_rating"),
            "worst_rating": (r["aggregate_rating"] or {}).get("worst_rating"),
            "rating_count": (r["aggregate_rating"] or {}).get("rating_count"),
            "review_count": (r["aggregate_rating"] or {}).get("review_count"), "observed_on": r["provenance"]["observed_on"],
            "captured_at": r["provenance"]["captured_at"], "validation_status": r["validation"]["status"],
            "dedup_hash": r["dedup_hash"], "description": r["description"], "method_summary": r["method_summary"],
            "content_status": r["content_status"]})
        ingredient_rows.extend({"recipe_id": r["recipe_id"], **i} for i in r["ingredients"])
        step_rows.extend({"recipe_id": r["recipe_id"], **step} for step in r["steps"])
        nutrition_rows.extend({"recipe_id": r["recipe_id"], "nutrient_or_basis": k, "observed_value": v,
                               "source_url": r["canonical_url"]} for k, v in (r["nutrition"] or {}).items())
    catalogue = {"schema_version": SCHEMA_VERSION, "parser_version": VERSION, "usage": usage,
                 "rights_reference": rights_reference, "record_count": len(recipes), "recipes": recipes}
    j = lambda obj: (json.dumps(obj, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode()
    recipe_columns = ["recipe_id", "canonical_url", "title", "categories", "diets", "servings", "prep_minutes", "cook_minutes", "total_minutes", "difficulty", "ingredient_count", "rating_value", "best_rating", "worst_rating", "rating_count", "review_count", "observed_on", "captured_at", "validation_status", "dedup_hash", "description", "method_summary", "content_status"]
    ingredient_columns = ["recipe_id", "ingredient_id", "position", "original_text", "source_name", "canonical_name", "canonical_match_status", "quantity", "quantity_text", "unit", "preparation", "parse_status"]

    return {"catalogue.json": j(catalogue), "errors.json": j(errors),
            "recipes.csv": csv_bytes(recipe_columns, recipe_rows), "ingredients.csv": csv_bytes(ingredient_columns, ingredient_rows),
            "steps.csv": csv_bytes(["recipe_id", "step_id", "position", "section", "name", "text"], step_rows),
            "nutrition.csv": csv_bytes(["recipe_id", "nutrient_or_basis", "observed_value", "source_url"], nutrition_rows)}


def reject_symlink_ancestors(path):
    if any(p.is_symlink() for p in [path, *path.parents]):
        raise ImportProblem("symlink_path_not_allowed")


def resume_manifest(out, fingerprint):
    reject_symlink_ancestors(out)
    manifest_path = out / "manifest.json"
    if not manifest_path.is_file() or manifest_path.stat().st_size > 1000000:
        raise ImportProblem("resume_missing_or_invalid_manifest")
    manifest = parse_json(manifest_path.read_text("utf-8"))
    if not isinstance(manifest, dict) or manifest.get("input_fingerprint") != fingerprint or manifest.get("parser_version") != VERSION or manifest.get("complete") is not True:
        raise ImportProblem("resume_manifest_mismatch")
    if any(type(manifest.get(k)) is not int or manifest[k] < 0 for k in ("record_count", "ingredient_count", "rejected_count", "duplicate_count")):
        raise ImportProblem("resume_invalid_counts")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict) or set(artifacts) != OUTPUT_NAMES:
        raise ImportProblem("resume_artifact_set_mismatch")
    for name, details in artifacts.items():
        path = out / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_OUTPUT:
            raise ImportProblem("resume_artifact_missing_or_unsafe")
        if not isinstance(details, dict) or digest(path.read_bytes()) != details.get("sha256") or path.stat().st_size != details.get("bytes"):
            raise ImportProblem("resume_artifact_hash_mismatch")
    return manifest


def atomic_new_file(path, content):
    # Hard-link publication is atomic and refuses to overwrite any existing path.
    fd, temp = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temp, path)
    finally:
        os.unlink(temp)


def run(inputs, out, usage, rights_reference, resume=False, include_authorized_text=False, text_rights_reference=None):
    if usage not in {"synthetic", "internal-sample", "authorized"}:
        raise ImportProblem("invalid_usage")
    rights_reference = txt(rights_reference, 1000)
    if not rights_reference or not rights_reference.strip():
        raise ImportProblem("rights_reference_required")
    if include_authorized_text and (usage != "authorized" or not txt(text_rights_reference, 1000) or not str(text_rights_reference).strip()):
        raise ImportProblem("authorized_text_requires_authorized_usage_and_text_rights_reference")
    if text_rights_reference and not include_authorized_text:
        raise ImportProblem("text_rights_reference_requires_include_authorized_text")
    if not 1 <= len(inputs) <= 100:
        raise ImportProblem("input_file_count_limit")
    out = Path(os.path.abspath(out))
    reject_symlink_ancestors(out)
    if not out.parent.is_dir():
        raise ImportProblem("output_parent_must_exist")
    sources, batches, errors = [], [], []
    total_bytes, total_records = 0, 0
    for path in sorted({Path(os.path.abspath(p)) for p in inputs}, key=str):
        reject_symlink_ancestors(path)
        if out == path or out in path.parents:
            raise ImportProblem("input_inside_output_directory")
        try:
            raw = read_bytes_limited(path)
            total_bytes += len(raw)
            if total_bytes > MAX_TOTAL:
                raise ImportProblem("total_input_limit")
            source = {"name": path.name, "sha256": digest(raw), "bytes": len(raw)}
            sources.append(source)
            _, records = read_input(path, raw)
            total_records += len(records)
            if total_records > MAX_RECORDS:
                raise ImportProblem("total_input_limit")
            batches.append((source, records))
        except ImportProblem as exc:
            if str(exc) == "total_input_limit":
                raise
            errors.append({"source_file": path.name, "record_index": None, "severity": "error", "code": str(exc)})
    fingerprint = digest({"sources": sources, "input_errors": errors, "usage": usage, "rights_reference": rights_reference, "version": VERSION,
                          "include_authorized_text": include_authorized_text, "text_rights_reference": text_rights_reference})
    if out.exists():
        if not resume:
            raise ImportProblem("output_exists_use_new_directory_or_verified_resume")
        return resume_manifest(out, fingerprint), True
    if resume:
        raise ImportProblem("resume_output_does_not_exist")
    imported = datetime.now(timezone.utc).isoformat()
    candidates = {}
    normalized_bytes = 0
    for source, records in batches:
        for index, data in enumerate(records, 1):
            try:
                recipe = normalize(data, source, index, imported, usage, rights_reference, include_authorized_text, text_rights_reference)
                normalized_bytes += len(compact(recipe).encode())
                if normalized_bytes > MAX_OUTPUT // 3:
                    raise ImportProblem("normalized_output_limit")
                candidates.setdefault(recipe["canonical_url"], []).append(recipe)
            except ImportProblem as exc:
                if str(exc) == "normalized_output_limit":
                    raise
                errors.append({"source_file": source["name"], "record_index": index, "severity": "error", "code": str(exc)})
    recipes = []
    for url in sorted(candidates):
        group = candidates[url]
        # Deterministic order independent of input argument order, avoiding mixed-source field merges.
        group.sort(key=lambda r: (-len(r["ingredients"]), r["dedup_hash"], r["provenance"]["source_file_sha256"], r["provenance"]["source_record_index"]))
        winner = group[0]
        if len(group) > 1:
            winner["validation"]["warnings"].append("duplicate_url_records_review_winner_and_source")
            winner["validation"]["status"] = "review_required"
            for discarded in group[1:]:
                errors.append({"source_file": discarded["provenance"]["source_file"], "record_index": discarded["provenance"]["source_record_index"],
                    "severity": "warning", "code": "duplicate_canonical_url", "recipe_id": winner["recipe_id"],
                    "kept_dedup_hash": winner["dedup_hash"], "discarded_dedup_hash": discarded["dedup_hash"]})
        recipes.append(winner)
    payloads = output_payloads(recipes, errors, usage, rights_reference)
    if sum(len(v) for v in payloads.values()) > MAX_OUTPUT:
        raise ImportProblem("output_size_limit")
    manifest = {"schema_version": SCHEMA_VERSION, "parser_version": VERSION, "complete": True,
                "created_at": imported, "input_fingerprint": fingerprint, "sources": sources, "usage": usage,
                "rights_reference": rights_reference, "record_count": len(recipes),
                "include_authorized_text": include_authorized_text, "text_rights_reference": text_rights_reference,
                "ingredient_count": sum(len(r["ingredients"]) for r in recipes),
                "rejected_count": sum(e["severity"] == "error" for e in errors),
                "duplicate_count": sum(e["code"] == "duplicate_canonical_url" for e in errors),
                "artifacts": {k: {"sha256": digest(v), "bytes": len(v)} for k, v in payloads.items()}}
    # Exclusively claim a fresh directory. Never delete, truncate or replace unknown files.
    out.mkdir(mode=0o700)
    for name, payload in payloads.items():
        atomic_new_file(out / name, payload)
    atomic_new_file(out / "manifest.json", (json.dumps(manifest, indent=2) + "\n").encode())
    return manifest, False


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="+", type=Path, help="Local .json, .jsonld or .csv files; never URLs")
    parser.add_argument("--output", required=True, type=Path, help="New output directory, or completed directory with --resume")
    parser.add_argument("--usage", required=True, choices=["synthetic", "internal-sample", "authorized"])
    parser.add_argument("--rights-reference", required=True, help="Nonsecret licence/export permission reference, or explicit sample limitation")
    parser.add_argument("--resume", action="store_true", help="Verify existing input/output hashes and return the completed run unchanged")
    parser.add_argument("--include-authorized-text", action="store_true", help="Import supplied description and ordered instructions only with explicit text rights")
    parser.add_argument("--text-rights-reference", help="Nonsecret reference specifically authorizing use of supplied recipe prose")
    args = parser.parse_args(argv)
    try:
        manifest, resumed = run(args.inputs, args.output, args.usage, args.rights_reference, args.resume,
                                args.include_authorized_text, args.text_rights_reference)
        print(json.dumps({"status": "verified_resume" if resumed else "complete", "records": manifest["record_count"],
                          "ingredients": manifest["ingredient_count"], "rejected": manifest["rejected_count"], "duplicates": manifest["duplicate_count"]}))
        return 2 if manifest["rejected_count"] else 0
    except (ImportProblem, OSError) as exc:
        # Do not echo raw recipe input, credentials or filesystem exception details.
        print(json.dumps({"status": "blocked", "code": str(exc) if isinstance(exc, ImportProblem) else "filesystem_error"}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
