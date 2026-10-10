#!/usr/bin/env python3
"""Permission-based public recipe collection. No login, private APIs or bypasses."""
from __future__ import annotations

import argparse
import email.utils
import hashlib
import json
import math
import re
import sys
import time
import urllib.error
import urllib.request
import urllib.robotparser
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit, parse_qsl, urlencode, urlunsplit

import importer

VERSION = "1.0.0"
ORIGIN = "https://www.cherrypick.co"
HOSTS = {"www.cherrypick.co", "cherrypick.co"}
USER_AGENT = "PantriRecipeCollector/1.0"
MAX_RESPONSE = 5 * 1024 * 1024
MAX_URLS = 10000
MAX_SITEMAPS = 100


def safe_url(url):
    result = importer.canonical_url(url)
    parsed = urlsplit(result)
    if parsed.scheme != "https" or parsed.hostname not in HOSTS or parsed.port not in (None, 443):
        raise importer.ImportProblem("collector_url_outside_approved_origin")
    return result


def recipe_url(url):
    result = safe_url(url)
    if not re.fullmatch(r"/recipes/[A-Za-z0-9_-]+", urlsplit(result).path):
        raise importer.ImportProblem("not_public_recipe_page")
    parsed = urlsplit(result)
    query = [(k,v) for k,v in parse_qsl(parsed.query,keep_blank_values=True) if not (k == "slug" and v == parsed.path.split("/")[-1])]
    result = urlunsplit((parsed.scheme,parsed.netloc,parsed.path,urlencode(query),""))
    return result


class SameOriginRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, client=None):
        self.client=client

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = safe_url(urljoin(req.full_url, newurl))
        # Never follow a recipe into sign-in, payment or unrelated account routes.
        if urlsplit(req.full_url).path.startswith("/recipes/"):
            recipe_url(target)
        if self.client and self.client.robot and not self.client.robot.can_fetch(USER_AGENT,target):
            raise importer.ImportProblem("robots_disallows_redirect_target")
        return super().redirect_request(req, fp, code, msg, headers, target)


class PublicClient:
    def __init__(self, delay=2.0, retries=2):
        if not math.isfinite(delay) or delay < 2 or retries not in range(0, 3):
            raise importer.ImportProblem("unsafe_rate_or_retry_setting")
        self.delay, self.retries, self.last_request = delay, retries, 0.0
        self.opener = urllib.request.build_opener(SameOriginRedirect(self))
        self.robot = None
        self.requests = 0

    def get(self, url, robots_request=False):
        url = safe_url(url)
        if not robots_request and (self.robot is None or not self.robot.can_fetch(USER_AGENT, url)):
            raise importer.ImportProblem("robots_disallows_url")
        for attempt in range(self.retries + 1):
            time.sleep(max(0.0, self.last_request + self.delay - time.monotonic()))
            self.last_request = time.monotonic()
            self.requests += 1
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/ld+json,application/xml,text/xml,text/plain", "Accept-Encoding": "identity"})
            try:
                with self.opener.open(request, timeout=30) as response:
                    safe_url(response.geturl())
                    if response.status != 200:
                        raise importer.ImportProblem("unexpected_http_status")
                    if response.headers.get("Content-Encoding", "identity").lower() not in {"", "identity"}:
                        raise importer.ImportProblem("unsupported_content_encoding")
                    length = response.headers.get("Content-Length")
                    if length and (not length.isdigit() or int(length) > MAX_RESPONSE):
                        raise importer.ImportProblem("http_response_size_limit")
                    raw = response.read(MAX_RESPONSE + 1)
                    if len(raw) > MAX_RESPONSE:
                        raise importer.ImportProblem("http_response_size_limit")
                    charset = response.headers.get_content_charset() or "utf-8"
                    try:
                        text = raw.decode(charset)
                    except (UnicodeError, LookupError):
                        raise importer.ImportProblem("http_response_encoding") from None
                    return text, response.geturl()
            except urllib.error.HTTPError as exc:
                if exc.code == 404 and robots_request:
                    return "User-agent: *\nAllow: /\n", url
                if exc.code in (401, 403):
                    raise importer.ImportProblem("access_denied_stop_no_bypass") from None
                if exc.code not in (429, 500, 502, 503, 504) or attempt == self.retries:
                    raise importer.ImportProblem("http_" + str(exc.code)) from None
                wait = max(self.delay, 2 ** (attempt + 2))
                retry_after = exc.headers.get("Retry-After")
                if retry_after:
                    try:
                        wait = max(wait, float(retry_after))
                    except ValueError:
                        try:
                            wait = max(wait, email.utils.parsedate_to_datetime(retry_after).timestamp() - time.time())
                        except (TypeError, ValueError, OverflowError):
                            raise importer.ImportProblem("invalid_retry_after_stop") from None
                if wait > 300:
                    raise importer.ImportProblem("long_retry_after_stop")
                time.sleep(wait)
            except (urllib.error.URLError, TimeoutError):
                if attempt == self.retries:
                    raise importer.ImportProblem("network_error_after_bounded_retries") from None
                time.sleep(max(self.delay, 2 ** (attempt + 2)))
        raise importer.ImportProblem("retry_limit")

    def load_robots(self):
        text, url = self.get(ORIGIN + "/robots.txt", robots_request=True)
        robot = urllib.robotparser.RobotFileParser(url)
        robot.parse(text.splitlines())
        self.robot = robot
        delay = robot.crawl_delay(USER_AGENT)
        rate = robot.request_rate(USER_AGENT)
        if delay:
            self.delay = max(self.delay, delay)
        if rate and rate.requests:
            self.delay = max(self.delay, rate.seconds / rate.requests)
        if self.delay > 300:
            raise importer.ImportProblem("robots_rate_requires_manual_scheduling")
        return [safe_url(url) for url in (robot.site_maps() or [ORIGIN + "/sitemap.xml"])], importer.digest(text.encode())


def sitemap_links(text):
    if "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
        raise importer.ImportProblem("unsafe_sitemap_xml")
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        raise importer.ImportProblem("invalid_sitemap_xml") from None
    kind = root.tag.split("}")[-1]
    if kind not in {"sitemapindex", "urlset"}:
        raise importer.ImportProblem("unknown_sitemap_type")
    urls = [node.text.strip() for node in root.iter() if node.tag.split("}")[-1] == "loc" and node.text]
    if len(urls) > MAX_URLS:
        raise importer.ImportProblem("sitemap_url_limit")
    return kind, urls


def discover(client, initial):
    pending, seen, recipes = list(initial), set(), set()
    while pending:
        url = safe_url(pending.pop(0))
        if url in seen:
            continue
        if len(seen) >= MAX_SITEMAPS:
            raise importer.ImportProblem("sitemap_count_limit")
        seen.add(url)
        text, _ = client.get(url)
        kind, links = sitemap_links(text)
        for link in links:
            target = safe_url(link)
            if kind == "sitemapindex":
                pending.append(target)
            else:
                try:
                    recipes.add(recipe_url(target))
                except importer.ImportProblem as exc:
                    if str(exc) != "not_public_recipe_page":
                        raise
            if len(recipes) > MAX_URLS:
                raise importer.ImportProblem("discovered_recipe_limit")
    if not recipes:
        raise importer.ImportProblem("no_public_recipe_urls_discovered")
    return sorted(recipes), sorted(seen)


class RecipeHTMLParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.scripts = []
        self.current = None
        self.root = HTMLNode("root", {}, None)
        self.stack = [self.root]
        self.nodes = []
        self.canonical = None

    def handle_starttag(self, tag, attrs):
        if len(self.stack)>100 or len(self.nodes)>100000:
            raise importer.ImportProblem("html_structure_limit")
        attrs = dict(attrs)
        node = HTMLNode(tag, attrs, self.stack[-1])
        self.stack[-1].parts.append(node)
        self.nodes.append(node)
        if tag not in {"img","br","hr","meta","link","input","source","area","base","embed","param","track","wbr"}:
            self.stack.append(node)
        if tag == "link" and attrs.get("rel", "").lower() == "canonical":
            self.canonical = attrs.get("href")
        if tag.lower() == "script":
            if (attrs.get("type") or "").lower().split(";")[0].strip() == "application/ld+json":
                self.current = []

    def handle_data(self, data):
        self.stack[-1].parts.append(data)
        if self.current is not None:
            self.current.append(data)

    def handle_endtag(self, tag):
        if tag.lower() == "script" and self.current is not None:
            self.scripts.append("".join(self.current))
            self.current = None
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                self.stack = self.stack[:index]
                break


class HTMLNode:
    def __init__(self, tag, attrs, parent):
        self.tag, self.attrs, self.parent, self.parts = tag, attrs, parent, []

    def text(self):
        return " ".join(p.text() if isinstance(p, HTMLNode) else p for p in self.parts)

    def descendants(self):
        for part in self.parts:
            if isinstance(part, HTMLNode):
                yield part
                yield from part.descendants()


UNIT_PATTERN = r"(?:small\s+bunch|large\s+bunch|bunches|bunch|tablespoons?|teaspoons?|tbsps?|tsps?|kilograms?|grams?|millilitres?|milliliters?|litres?|liters?|kgs?|g|ml|l|cups?|ounces?|oz|lbs?|pounds?|pinches|pinch|cloves?|blocks?|slices?|handfuls?|sprigs?|stalks?|cm)"
QUANTITY_PATTERN = r"(?:(?:\d+\s*)?[¼½¾⅐⅑⅒⅓⅔⅕⅖⅗⅘⅙⅚⅛⅜⅝⅞]|\d+\s+\d+\s*/\s*\d+|\d+(?:\.\d+)?(?:\s*/\s*\d+)?(?:\s*[-–]\s*\d+(?:\.\d+)?)?)"
DIET_LABELS = {"vegan","vegetarian","pescatarian","dairy free","gluten free","nut free","egg free","low carb","low calorie","high protein"}


def parse_ingredient_text(value):
    original = importer.txt(value)
    base, separator, preparation = original.partition(",")
    base = base.strip()
    quantity, unit, uncertain = None, None, False
    match = re.match(r"^(" + QUANTITY_PATTERN + r")\s*(.*)$", base)
    if match:
        quantity, base = match.groups()
        # Preserve composite quantities rather than incorrectly multiplying or treating pack amounts as portions.
        if re.match(r"^(?:x|×)\s*\d",base,re.I):
            uncertain = True
            quantity = None
            base = original.partition(",")[0].strip()
        else:
            match = re.match(r"^(" + UNIT_PATTERN + r")(?:\s+of)?(?:\s+|$)(.*)$", base, re.I)
            if match:
                unit, base = match.groups()
    else:
        match = re.match(r"^(" + UNIT_PATTERN + r")(?:\s+of)?\s+(.*)$", base, re.I)
        if match:
            unit, base = match.groups()
    # '4 Garlic Cloves' explicitly places the count unit after the ingredient name.
    suffix = re.fullmatch(r"(.+?)\s+(Cloves?|Sprigs?)", base, re.I)
    if suffix and quantity and not unit:
        base, unit = suffix.groups()
    if not base or re.search(r"\d\s*(?:x|×)|\b(?:cans?|tins?)\b", base, re.I):
        uncertain = True
    return {"original_text": original, "source_name": base or None, "quantity_text": quantity,
        "unit": unit, "preparation": preparation.strip() if separator else None,
        "parse_status":"rule_parsed_reviewable","canonical_match_status":"needs_form_review" if uncertain else "local_label_only_master_not_loaded"}


def page_metadata(parser, author_names=()):
    heading = next((n for n in parser.nodes if n.tag == "h1"), None)
    tags, difficulty = [], None
    if heading and heading.parent:
        header_texts=[" ".join(node.text().split()) for node in heading.parent.descendants() if node.tag in {"a","span"}]
        author_names=set(author_names)|{text[3:].strip().lower() for text in header_texts if text.lower().startswith("by ")}
        for node in heading.parent.descendants():
            text = " ".join(node.text().split())
            if node.tag in {"a","span"} and 0 < len(text) <= 100 and re.fullmatch(r"[A-Za-z][A-Za-z &+-]*",text) and text.lower() not in {"star","reviews","review","cherrypick",*author_names} and not text.lower().startswith("by "):
                tags.append(text)
        if heading.parent.parent:
            top_list = next((n for n in heading.parent.parent.parts if isinstance(n,HTMLNode) and n.tag == "ul"),None)
            if top_list:
                match = re.search(r"\b(Easy|Medium|Hard|Moderate|Advanced)\s+level\b", " ".join(top_list.text().split()), re.I)
                if match:
                    difficulty = match.group(1)
    summary = next((n for n in parser.nodes if n.attrs.get("id") == "recipe-reviews-section"),None)
    rating = None
    if summary:
        for node in summary.descendants():
            if node.tag != "p":
                continue
            match = re.search(r"(\d+(?:\.\d+)?)\s+out of\s+(\d+(?:\.\d+)?)\s*\(([\d,]+)\s+ratings?,\s*([\d,]+)\s+with reviews?\)"," ".join(node.text().split()))
            if match:
                value, best, count, reviews = match.groups()
                rating={"rating_value":float(value),"best_rating":float(best),"rating_count":int(count.replace(",","")),"review_count":int(reviews.replace(",",""))}
                break
    return {"categories":sorted(set(tags)),"diets":sorted({t for t in tags if t.lower() in DIET_LABELS}),"difficulty":difficulty,"aggregate_rating":rating}


def parse_public_recipe(html, page_url, captured_at, rights_reference, text_rights_reference):
    # Only public HTML returned for the recipe page is inspected. No API calls.
    if re.search(r"<title[^>]*>\s*(?:just a moment|access denied|verify you are human|attention required)", html, re.I):
        raise importer.ImportProblem("access_challenge_stop_no_bypass")
    parser = RecipeHTMLParser()
    parser.feed(html)
    candidates = []
    for script in parser.scripts:
        document = importer.parse_json(script)
        # recipe_nodes ignores non-Recipe typed nodes. Require an explicit Recipe type here.
        for candidate in importer.recipe_nodes(document):
            types = importer.as_list(candidate.get("@type"))
            if any(str(t).split("/")[-1] == "Recipe" for t in types):
                candidates.append(candidate)
    if len(candidates) != 1:
        raise importer.ImportProblem("expected_one_public_recipe_jsonld")
    candidate = dict(candidates[0])
    source_url = recipe_url(page_url)
    if parser.canonical and recipe_url(urljoin(source_url,parser.canonical)) != source_url:
        raise importer.ImportProblem("html_canonical_url_mismatch")
    if candidate.get("url"):
        declared = candidate["url"]
        if isinstance(declared, dict):
            declared = declared.get("@id")
        if recipe_url(urljoin(source_url, declared)) != source_url:
            raise importer.ImportProblem("recipe_canonical_url_mismatch")
    candidate["canonical_url"] = source_url
    author_names = {str(a.get("name", "")).lower() if isinstance(a,dict) else str(a).lower() for a in importer.as_list(candidate.get("author"))}
    metadata = page_metadata(parser, author_names)
    candidate.update({k:v for k,v in metadata.items() if v is not None})
    if not metadata["aggregate_rating"]:
        rating = dict(candidate.get("aggregateRating") or {})
        # Provider-specific field correction: this site's JSON-LD reviewCount means all ratings.
        if "reviewCount" in rating:
            rating["ratingCount"] = rating.pop("reviewCount")
        candidate["aggregateRating"] = rating
    ingredient_texts = candidate.get("recipeIngredient") or []
    candidate["ingredients"] = [parse_ingredient_text(text) for text in ingredient_texts]
    candidate["field_provenance"] = [
        {"fields":["description","steps","servings","timings"],"source_url":source_url,"source_section":"Public Recipe JSON-LD"},
        {"fields":["ingredients"],"source_url":source_url,"source_section":"Public Recipe JSON-LD ingredient strings","transformation":"Conservative quantity/unit/name parsing; original text retained; no master ingredient match or quantity conversion."},
        {"fields":["categories","diets","difficulty"],"source_url":source_url,"source_section":"Recipe heading local badges; excludes global navigation"},
        {"fields":["aggregate_rating"],"source_url":source_url,"source_section":"Visible reviews summary when present; otherwise provider-corrected JSON-LD ratingCount; written reviews unknown without visible summary"}]
    candidate["captured_at"] = captured_at
    candidate["observed_on"] = captured_at[:10]
    candidate["provenance"] = {"captured_at": captured_at, "observed_on": captured_at[:10],
        "capture_method": "Permission-based public recipe HTML JSON-LD; no private API or login.",
        "permission_status": rights_reference,"source_page_sha256":importer.digest(html.encode())}
    record = importer.normalize(candidate, {"name": "public-page.html", "sha256": importer.digest(html.encode())}, 1,
                                captured_at, "authorized", rights_reference, True, text_rights_reference)
    if not record["ingredients"] or not record["steps"]:
        raise importer.ImportProblem("pilot_missing_ingredients_or_ordered_steps")
    return record


def collect(args):
    if not args.rights_reference.strip() or not args.text_rights_reference.strip():
        raise importer.ImportProblem("collection_rights_references_required")
    if not 1 <= args.limit <= MAX_URLS:
        raise importer.ImportProblem("collection_limit_out_of_range")
    output = Path(args.output).absolute()
    importer.reject_symlink_ancestors(output)
    if not output.parent.is_dir():
        raise importer.ImportProblem("collection_output_parent_missing")
    config = {"collector_version":VERSION,"rights_reference":args.rights_reference,"text_rights_reference":args.text_rights_reference}
    client = PublicClient(args.delay_seconds, args.retries)
    initial, robots_hash = client.load_robots()
    if output.exists():
        if not args.resume:
            raise importer.ImportProblem("collection_output_exists_use_verified_resume")
        importer.reject_symlink_ancestors(output)
        config_file = output / "run-config.json"
        queue_file = output / "queue.json"
        if not config_file.is_file() or not queue_file.is_file() or max(config_file.stat().st_size,queue_file.stat().st_size)>2*1024*1024:
            raise importer.ImportProblem("collection_resume_metadata_invalid")
        old_config = importer.parse_json(config_file.read_text("utf-8"))
        if not isinstance(old_config,dict) or any(old_config.get(k)!=v for k,v in config.items()) or old_config.get("queue_sha256")!=importer.digest(queue_file.read_bytes()):
            raise importer.ImportProblem("collection_resume_config_mismatch")
        queue_data = importer.parse_json(queue_file.read_text("utf-8"))
        if not isinstance(queue_data,dict) or not isinstance(queue_data.get("urls"),list) or not 1<=len(queue_data["urls"])<=MAX_URLS or not isinstance(queue_data.get("sitemaps"),list):
            raise importer.ImportProblem("collection_resume_queue_invalid")
        urls = [recipe_url(url) for url in queue_data["urls"]]
        sitemaps = queue_data["sitemaps"]
    else:
        if args.resume:
            raise importer.ImportProblem("collection_resume_output_missing")
        if args.urls_file:
            source = Path(args.urls_file).absolute()
            importer.reject_symlink_ancestors(source)
            if not source.is_file() or source.stat().st_size > 2 * 1024 * 1024:
                raise importer.ImportProblem("invalid_urls_file")
            urls = sorted({recipe_url(line.strip()) for line in source.read_text("utf-8").splitlines() if line.strip()})
            sitemaps = []
        else:
            urls, sitemaps = discover(client, initial)
        if not urls or len(urls) > MAX_URLS:
            raise importer.ImportProblem("collection_queue_limit")
        output.mkdir(mode=0o700)
        (output / "records").mkdir(mode=0o700)
        (output / "snapshots").mkdir(mode=0o700)
        queue_payload=(json.dumps({"discovered_count":len(urls),"urls":urls,"sitemaps":sitemaps},indent=2)+"\n").encode()
        importer.atomic_new_file(output / "queue.json",queue_payload)
        importer.atomic_new_file(output / "run-config.json",(json.dumps({**config,"queue_sha256":importer.digest(queue_payload)},indent=2)+"\n").encode())
    queue, records, errors = urls[:args.limit], [], []
    record_dir = output / "records"
    for index, url in enumerate(queue, 1):
        rid = "cp_"+hashlib.sha256(url.encode()).hexdigest()[:16]
        record_file = record_dir / (rid+".json")
        try:
            if record_file.exists():
                importer.reject_symlink_ancestors(record_file)
                if record_file.stat().st_size>MAX_RESPONSE:
                    raise importer.ImportProblem("collection_cache_size_limit")
                cached=importer.parse_json(record_file.read_text("utf-8"))
                if not isinstance(cached,dict):
                    raise importer.ImportProblem("collection_cache_metadata_invalid")
                record=cached.get("record")
                if cached.get("url")!=url or not isinstance(record,dict) or cached.get("record_sha256")!=importer.digest(record) or record.get("canonical_url")!=url:
                    raise importer.ImportProblem("collection_cache_hash_mismatch")
            else:
                html, final_url = client.get(url)
                captured = datetime.now(timezone.utc).isoformat()
                record = parse_public_recipe(html, final_url, captured, args.rights_reference, args.text_rights_reference)
                payload={"url":url,"record":record,"record_sha256":importer.digest(record)}
                importer.atomic_new_file(record_file,(json.dumps(payload,indent=2,ensure_ascii=False)+"\n").encode())
            records.append(record)
            print(json.dumps({"progress":index,"selected":len(queue),"accepted":len(records),"rejected":len(errors)}),flush=True)
        except importer.ImportProblem as exc:
            errors.append({"url":url,"code":str(exc)})
            if str(exc) != "http_404":
                # Stop at access barriers, schema drift or malformed records for review.
                break
    snapshot=output / "snapshots" / str(time.time_ns())
    snapshot.mkdir(mode=0o700)
    source_exports=[]
    exports={}
    for start in range(0,len(records),200):
        source_export=snapshot / ("authorized-export.json" if len(records)<=200 else "authorized-export-"+str(start//200+1).zfill(3)+".json")
        payload=(json.dumps({"recipes":records[start:start+200]},indent=2,ensure_ascii=False)+"\n").encode()
        if len(payload)>importer.MAX_FILE:
            raise importer.ImportProblem("collection_export_chunk_too_large")
        importer.atomic_new_file(source_export,payload)
        source_exports.append(source_export)
        exports[source_export.name]={"sha256":importer.digest(payload),"bytes":len(payload)}
    importer.atomic_new_file(snapshot / "collection-errors.json",(json.dumps(errors,indent=2)+"\n").encode())
    manifest={"collector_version":VERSION,"complete":len(records)==len(queue) and not errors,
        "rights_reference":args.rights_reference,"text_rights_reference":args.text_rights_reference,
        "robots_sha256":robots_hash,"sitemaps":sitemaps,"discovered_count":len(urls),"selected_count":len(queue),
        "accepted_count":len(records),"errors":errors,"http_requests":client.requests,
        "effective_delay_seconds":client.delay,"exports":exports}
    if records:
        imported,_=importer.run(source_exports,snapshot / "normalized","authorized",args.rights_reference,
                                include_authorized_text=True,text_rights_reference=args.text_rights_reference)
        if imported["rejected_count"] or imported["record_count"]!=len(records):
            manifest["complete"]=False
            errors.append({"code":"final_normalized_record_count_mismatch"})
    importer.atomic_new_file(snapshot / "collection-manifest.json",(json.dumps(manifest,indent=2)+"\n").encode())
    print(json.dumps({"status":"complete" if manifest["complete"] else "stopped_for_review","discovered":len(urls),"selected":len(queue),"accepted":len(records),"errors":errors,"snapshot":str(snapshot)}),flush=True)
    return 0 if manifest["complete"] else 2


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",required=True,type=Path)
    parser.add_argument("--rights-reference",required=True)
    parser.add_argument("--text-rights-reference",required=True)
    parser.add_argument("--limit",type=int,default=25,help="Default pilot: 25 public recipe pages; max 10,000")
    parser.add_argument("--resume",action="store_true",help="Verify cached records and continue the saved queue without refetching successful pages")
    parser.add_argument("--urls-file",type=Path,help="Optional private list of approved public recipe URLs")
    parser.add_argument("--delay-seconds",type=float,default=2.0,help="At least two seconds; robots may require longer")
    parser.add_argument("--retries",type=int,default=2,choices=range(0,3))
    args=parser.parse_args(argv)
    try:
        return collect(args)
    except (importer.ImportProblem,OSError) as exc:
        print(json.dumps({"status":"blocked","code":str(exc) if isinstance(exc,importer.ImportProblem) else "filesystem_error"}),file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
