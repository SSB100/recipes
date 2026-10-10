"""Synthetic fixtures only. No live website access or real recipe collection."""
import copy
import csv
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import importer as imp


def fixture():
    return {"canonical_url": "https://recipes.example.test/synthetic-lentil-bowl", "title": "SYNTHETIC TEST: Lentil Bowl",
            "servings": 2, "prep_minutes": 10, "total_minutes": 30,
            "categories": ["Test fixture"], "diets": ["Source label for testing only"],
            "ingredients": [{"name": "Test lentils", "original_text": "200g Test lentils", "quantity_text": "200", "unit": "g"}],
            "captured_at": None, "observed_on": "2026-01-01"}


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / "fixture.json"

    def tearDown(self):
        self.tmp.cleanup()

    def save(self, data=None, path=None):
        path = path or self.source
        path.write_text(json.dumps(fixture() if data is None else data), encoding="utf-8")
        return path

    def run_import(self, data=None, out="out", **kwargs):
        self.save(data)
        return imp.run([self.source], self.root / out, "synthetic", "Self-authored test fixture", **kwargs)

    def recipe(self, out="out"):
        return json.loads((self.root / out / "catalogue.json").read_text())["recipes"][0]

    def normalize(self, data):
        return imp.normalize(data, {"name": "synthetic.json", "sha256": "0" * 64}, 1,
                             "2026-01-01T00:00:00+00:00", "synthetic", "Self-authored test fixture")

    def test_complete_export_and_exact_unknowns(self):
        manifest, resumed = self.run_import()
        r = self.recipe()
        self.assertFalse(resumed)
        self.assertEqual(manifest["record_count"], 1)
        self.assertEqual(manifest["ingredient_count"], 1)
        self.assertIsNone(r["timings"]["cook_minutes"])
        self.assertIsNone(r["nutrition"])
        self.assertIsNone(r["allergens"])
        self.assertIsNone(r["provenance"]["captured_at"])
        self.assertEqual(r["provenance"]["capture_time_status"], "exact_timestamp_unknown")
        self.assertEqual(set(manifest["artifacts"]), imp.OUTPUT_NAMES)

    def test_stable_ids(self):
        r = self.normalize(fixture())
        expected = "cp_" + hashlib.sha256(fixture()["canonical_url"].encode()).hexdigest()[:16]
        self.assertEqual(r["recipe_id"], expected)
        self.assertEqual(r["ingredients"][0]["ingredient_id"], expected + "_i01")

    def test_url_canonicalization(self):
        self.assertEqual(imp.canonical_url("HTTPS://EXAMPLE.COM:443/r/?utm_source=x&b=2&a=1#top"), "https://example.com/r?a=1&b=2")
        self.assertNotEqual(imp.canonical_url("https://x.test/r?v=1"), imp.canonical_url("https://x.test/r?v=2"))

    def test_unsafe_urls(self):
        for url in ["file:///etc/passwd", "https://user:pass@x.test/r", "javascript:alert(1)", "https://x.test/r\n", "https://x.test:bad/r"]:
            with self.subTest(url=url), self.assertRaises(imp.ImportProblem):
                imp.canonical_url(url)

    def test_schema_jsonld_graph(self):
        data = {"@context": "https://schema.org", "@graph": [{"@type": "Organization", "name": "Ignored"},
            {"@type": ["Thing", "Recipe"], "url": "https://recipes.example.test/jsonld", "name": "SYNTHETIC JSON-LD",
             "prepTime": "PT10M", "cookTime": "PT1H30M", "totalTime": "PT2H", "recipeYield": "4 servings",
             "recipeIngredient": ["2 synthetic onions"], "recipeInstructions": "DO NOT PERSIST METHOD",
             "nutrition": {"calories": "400 kcal", "description": "DO NOT PERSIST DESCRIPTION"},
             "aggregateRating": {"ratingValue": 4.1, "ratingCount": 20, "bestRating": 5}}]}
        manifest, _ = self.run_import(data)
        self.assertEqual(manifest["record_count"], 1)
        r = self.recipe()
        self.assertEqual(r["timings"]["cook_minutes"], 90)
        self.assertIsNone(r["servings"])
        self.assertEqual(r["yield_text"], "4 servings")
        self.assertIsNone(r["ingredients"][0]["quantity_text"])
        self.assertEqual(r["nutrition"], {"calories": "400 kcal"})
        self.assertNotIn("DO NOT PERSIST", (self.root / "out" / "catalogue.json").read_text())

    def test_policy_omissions(self):
        data = fixture()
        for key in imp.FORBIDDEN_KEYS:
            data[key] = "COPYRIGHTED TEST SENTINEL"
        self.run_import(data)
        for file in (self.root / "out").iterdir():
            self.assertNotIn("COPYRIGHTED TEST SENTINEL", file.read_text(encoding="utf-8-sig"))
        self.assertEqual(set(self.recipe()["omitted_fields"]["input_keys_discarded_by_policy"]), imp.FORBIDDEN_KEYS)

    def test_canonical_labels_and_explicit_quantities(self):
        data = fixture()
        data["ingredients"][0]["name"] = "  Frozen   Test Lentils  "
        r = self.normalize(data)
        i = r["ingredients"][0]
        self.assertEqual(i["source_name"], "  Frozen   Test Lentils  ")
        self.assertEqual(i["canonical_name"], "frozen test lentils")
        self.assertEqual(i["quantity"], 200)
        self.assertEqual(i["canonical_match_status"], "local_label_only_master_not_loaded")

    def test_ambiguous_canonical_form_review(self):
        for name, unit in [("Rice", "g"), ("Hot Vegetable Stock", "ml"), ("Basil", "small bunch"), ("Tofu", "blocks")]:
            data = fixture()
            data["ingredients"][0].update({"name": name, "unit": unit})
            self.assertEqual(self.normalize(data)["ingredients"][0]["canonical_match_status"], "needs_form_review")

    def test_retailer_mappings_omitted(self):
        data = fixture()
        data["ingredients"][0]["uk_product_mapping"] = {"retailer": "OMIT SYNTHETIC SHOP", "pack_size_quantity": 500}
        data["retailer_pack_mappings"] = [{"retailer": "OMIT SYNTHETIC SHOP"}]
        self.run_import(data)
        r = self.recipe()
        self.assertNotIn("retailer_pack_mappings", r)
        self.assertEqual(r["ingredients"][0]["quantity"], 200)
        self.assertFalse((self.root / "out" / "retailer_pack_mappings.csv").exists())
        self.assertNotIn("OMIT SYNTHETIC SHOP", (self.root / "out" / "catalogue.json").read_text())

    def test_dedup_deterministic_and_conflict_report(self):
        one, two = fixture(), fixture()
        two["canonical_url"] += "/?utm_source=test#foo"
        two["title"] = "SYNTHETIC competing title"
        self.run_import([one, two])
        self.run_import([two, one], out="out2")
        self.assertEqual(self.recipe()["dedup_hash"], self.recipe("out2")["dedup_hash"])
        self.assertEqual(json.loads((self.root / "out" / "errors.json").read_text())[0]["code"], "duplicate_canonical_url")

    def test_resume_hash_verified_no_rewrite(self):
        self.run_import()
        manifest_path = self.root / "out" / "manifest.json"
        before = manifest_path.stat().st_mtime_ns
        _, resumed = imp.run([self.source], self.root / "out", "synthetic", "Self-authored test fixture", resume=True)
        self.assertTrue(resumed)
        self.assertEqual(before, manifest_path.stat().st_mtime_ns)
        (self.root / "out" / "recipes.csv").write_text("tampered")
        with self.assertRaisesRegex(imp.ImportProblem, "hash_mismatch"):
            imp.run([self.source], self.root / "out", "synthetic", "Self-authored test fixture", resume=True)

    def test_resume_rejects_changed_input(self):
        self.run_import()
        updated = fixture()
        updated["title"] = "SYNTHETIC changed input"
        self.save(updated)
        with self.assertRaisesRegex(imp.ImportProblem, "manifest_mismatch"):
            imp.run([self.source], self.root / "out", "synthetic", "Self-authored test fixture", resume=True)

    def test_resume_rejects_changed_invalid_input(self):
        self.source.write_text("{broken one")
        imp.run([self.source], self.root / "out", "synthetic", "Self-authored test fixture")
        self.source.write_text("{broken two")
        with self.assertRaisesRegex(imp.ImportProblem, "manifest_mismatch"):
            imp.run([self.source], self.root / "out", "synthetic", "Self-authored test fixture", resume=True)

    def test_resume_rejects_manifest_traversal(self):
        self.run_import()
        p = self.root / "out" / "manifest.json"
        data = json.loads(p.read_text())
        data["artifacts"]["../secret"] = data["artifacts"].pop("recipes.csv")
        p.write_text(json.dumps(data))
        with self.assertRaisesRegex(imp.ImportProblem, "artifact_set"):
            imp.run([self.source], self.root / "out", "synthetic", "Self-authored test fixture", resume=True)

    def test_never_overwrites_unknown_directory(self):
        out = self.root / "out"
        out.mkdir()
        (out / "keep.txt").write_text("KEEP")
        with self.assertRaisesRegex(imp.ImportProblem, "output_exists"):
            self.run_import()
        self.assertEqual((out / "keep.txt").read_text(), "KEEP")

    def test_symlink_refused(self):
        self.save()
        linked = self.root / "link.json"
        linked.symlink_to(self.source)
        with self.assertRaisesRegex(imp.ImportProblem, "symlink"):
            imp.run([linked], self.root / "out", "synthetic", "Test")

    def test_atomic_file_does_not_replace(self):
        p = self.root / "keep.txt"
        p.write_bytes(b"KEEP")
        with self.assertRaises(FileExistsError):
            imp.atomic_new_file(p, b"REPLACE")
        self.assertEqual(p.read_bytes(), b"KEEP")
        self.assertFalse(list(self.root.glob(".pending-*")))

    def test_csv_formula_defense(self):
        for value in ["=1+1", " +SUM(A1)", "-1+2", "@SUM(A1)", "\tcell", "\n=evil"]:
            self.assertTrue(imp.csv_safe(value).startswith("'"))
        self.assertEqual(imp.csv_safe(12), 12)
        data = fixture()
        data["title"] = " =HYPERLINK(\"evil\")"
        self.run_import(data)
        with (self.root / "out" / "recipes.csv").open(encoding="utf-8-sig", newline="") as f:
            self.assertTrue(next(csv.DictReader(f))["title"].startswith("'"))
        self.assertEqual(self.recipe()["title"], data["title"])

    def test_csv_input(self):
        p = self.root / "input.csv"
        with p.open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, ["canonical_url", "title", "ingredients", "categories"])
            w.writeheader()
            w.writerow({"canonical_url": fixture()["canonical_url"], "title": "SYNTHETIC CSV",
                        "ingredients": json.dumps(fixture()["ingredients"]), "categories": '["Test"]'})
        m, _ = imp.run([p], self.root / "out", "synthetic", "Test")
        self.assertEqual(m["ingredient_count"], 1)

    def test_malformed_csv_and_json_reported(self):
        for name, content in [("bad.csv", "title,title\nx,y\n"), ("bad.json", '{"title":1,"title":2}')]:
            p = self.root / name
            p.write_text(content)
            m, _ = imp.run([p], self.root / (name + ".out"), "synthetic", "Test")
            self.assertEqual(m["rejected_count"], 1)
            self.assertEqual(m["record_count"], 0)

    def test_record_rejection_without_raw_data_echo(self):
        data = fixture()
        data["canonical_url"] = "SECRET INVALID URL"
        m, _ = self.run_import([data, fixture()])
        self.assertEqual(m["record_count"], 1)
        self.assertEqual(m["rejected_count"], 1)
        self.assertNotIn("SECRET INVALID", (self.root / "out" / "errors.json").read_text())

    def test_bad_numeric_values(self):
        for value in [True, -1, float("inf"), "oops"]:
            with self.subTest(value=value), self.assertRaises(imp.ImportProblem):
                imp.number(value)
        with self.assertRaises(imp.ImportProblem):
            imp.number(1.5, integer=True)

    def test_bad_capture_date(self):
        for value in ["yesterday", "2026-01-01T10:00:00"]:
            data = fixture()
            data["captured_at"] = value
            with self.assertRaisesRegex(imp.ImportProblem, "capture_date"):
                self.normalize(data)

    def test_authorized_text_gate(self):
        self.save()
        for usage, reference in [("synthetic", "Owned"), ("internal-sample", "Owned"), ("authorized", None), ("authorized", " ")]:
            with self.subTest(usage=usage, reference=reference), self.assertRaisesRegex(imp.ImportProblem, "authorized_text_requires"):
                imp.run([self.source], self.root / "out", usage, "Test", include_authorized_text=True, text_rights_reference=reference)
        with self.assertRaisesRegex(imp.ImportProblem, "requires_include"):
            imp.run([self.source], self.root / "out", "authorized", "Test", text_rights_reference="Owned")
        self.assertFalse((self.root / "out").exists())

    def test_authorized_prose_and_ordered_steps(self):
        data = fixture()
        data.update({"description": "Self-authored synthetic prose.", "method_summary": "Self-authored synthetic overview.",
                     "recipeInstructions": [{"@type": "HowToSection", "name": "Synthetic section", "itemListElement": [
                         {"@type": "HowToStep", "name": "First", "text": "Self-authored synthetic first step."},
                         {"@type": "HowToStep", "text": "Self-authored synthetic second step."}]}],
                     "review_text": "NEVER COPY REVIEWS", "image": "NEVER COPY IMAGES"})
        self.save(data)
        imp.run([self.source], self.root / "out", "authorized", "Owned test fixture", include_authorized_text=True, text_rights_reference="Self-authored synthetic prose")
        r = self.recipe()
        self.assertEqual(r["description"], data["description"])
        self.assertEqual(r["content_status"], "authorized_text_imported")
        self.assertEqual([s["position"] for s in r["steps"]], [1, 2])
        self.assertEqual(r["steps"][0]["section"], "Synthetic section")
        self.assertEqual(r["provenance"]["text_rights_reference"], "Self-authored synthetic prose")
        self.assertNotIn("NEVER COPY", (self.root / "out" / "catalogue.json").read_text())
        self.assertEqual(len(list(csv.DictReader((self.root / "out" / "steps.csv").read_text(encoding="utf-8-sig").splitlines()))), 2)

    def test_description_and_steps_default_blank(self):
        data = fixture()
        data.update({"description": "Do not copy", "steps": [{"text": "Do not copy"}], "method_summary": "Do not copy"})
        self.run_import(data)
        r = self.recipe()
        self.assertIsNone(r["description"])
        self.assertIsNone(r["method_summary"])
        self.assertEqual(r["steps"], [])
        self.assertEqual(r["content_status"], "not_imported_requires_authorized_text")
        self.assertEqual(len((self.root / "out" / "steps.csv").read_text().splitlines()), 1)

    def test_instruction_string_kept_one_step(self):
        result = imp.normalize_steps("Synthetic first. Synthetic second.", "cp_test")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["step_id"], "cp_test_s01")

    def test_jsonld_id_url_fallback(self):
        data = fixture()
        del data["canonical_url"]
        data["@id"] = "https://recipes.example.test/id-fallback#recipe"
        self.assertEqual(self.normalize(data)["canonical_url"], "https://recipes.example.test/id-fallback")

    def test_source_time_no_default_rating_scale(self):
        data = fixture()
        data["aggregate_rating"] = {"rating_value": 4.5}
        data["captured_at"] = "2026-01-01T13:00:00+13:00"
        r = self.normalize(data)
        self.assertIsNone(r["aggregate_rating"]["best_rating"])
        self.assertEqual(r["provenance"]["captured_at"], data["captured_at"])

    def test_csv_rating_scale_and_counts_separate(self):
        data = fixture()
        data["aggregate_rating"] = {"rating_value":4.2,"best_rating":5,"worst_rating":1,"rating_count":100,"review_count":20}
        self.run_import(data)
        with (self.root / "out" / "recipes.csv").open(encoding="utf-8-sig", newline="") as f:
            row = next(csv.DictReader(f))
        for k, v in data["aggregate_rating"].items():
            self.assertEqual(row[k], str(v))

    def test_schema_key_contract(self):
        self.run_import()
        schema = json.loads((Path(imp.__file__).parent / "schema.json").read_text())
        catalogue = json.loads((self.root / "out" / "catalogue.json").read_text())
        self.assertEqual(set(catalogue), set(schema["required"]))
        r = catalogue["recipes"][0]
        for definition, value in [("recipe", r), ("ingredient", r["ingredients"][0]), ("provenance", r["provenance"])]:
            self.assertEqual(set(value), set(schema["$defs"][definition]["required"]))

    def test_thousand_synthetic_recipe_smoke(self):
        data = []
        for i in range(1000):
            row = fixture()
            row["canonical_url"] += "-" + str(i)
            data.append(row)
        manifest, _ = self.run_import(data)
        self.assertEqual(manifest["record_count"], 1000)
        self.assertEqual(manifest["ingredient_count"], 1000)

    def test_depth_nonfinite_and_no_recipe_limits(self):
        for raw in ['{"bad":NaN}', '{"bad":Infinity}']:
            with self.assertRaises(imp.ImportProblem):
                imp.parse_json(raw)
        obj = None
        for _ in range(40):
            obj = [obj]
        with self.assertRaises(imp.ImportProblem):
            imp.parse_json(json.dumps(obj))
        self.source.write_text('{"@graph":[{"@type":"Organization","name":"Synthetic only"}]}')
        with self.assertRaisesRegex(imp.ImportProblem, "no_recipe"):
            imp.read_input(self.source)

    def test_limits(self):
        self.save()
        with patch.object(imp, "MAX_FILE", 4):
            with self.assertRaisesRegex(imp.ImportProblem, "file_size"):
                imp.read_input(self.source)
        with patch.object(imp, "MAX_OUTPUT", 5):
            with self.assertRaisesRegex(imp.ImportProblem, "output_limit"):
                imp.run([self.source], self.root / "out", "synthetic", "Test")
        self.assertFalse((self.root / "out").exists())
        with patch.object(imp, "MAX_INGREDIENTS", 0):
            with self.assertRaisesRegex(imp.ImportProblem, "ingredient_array"):
                self.normalize(fixture())

    def test_no_remote_jsonld_resolution(self):
        data = fixture()
        data["@context"] = "https://not-fetched.example.test/schema"
        with patch("socket.socket", side_effect=AssertionError("No network permitted")):
            self.run_import(data)

    def test_cli_exit_codes(self):
        self.save()
        cli = Path(imp.__file__)
        command = [sys.executable, str(cli), str(self.source), "--output", str(self.root / "out"), "--usage", "synthetic", "--rights-reference", "Test fixture"]
        self.assertEqual(subprocess.run(command, capture_output=True).returncode, 0)
        self.assertEqual(subprocess.run(command, capture_output=True).returncode, 1)
        self.assertEqual(subprocess.run(command + ["--resume"], capture_output=True).returncode, 0)
        self.save({"title": "SYNTHETIC missing URL"})
        command[command.index(str(self.root / "out"))] = str(self.root / "invalid")
        self.assertEqual(subprocess.run(command, capture_output=True).returncode, 2)


if __name__ == "__main__":
    unittest.main()
