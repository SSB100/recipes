"""Offline collector tests. Every HTML recipe and ingredient here is synthetic."""
import json
import io
import tempfile
from argparse import Namespace
from contextlib import redirect_stdout
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import collector
import importer


def synthetic_html():
    data={"@type":"Recipe","name":"SYNTHETIC Recipe","url":"https://www.cherrypick.co/recipes/synthetic-test?slug=synthetic-test",
          "description":"Self-authored synthetic description.","recipeYield":"2","prepTime":"PT5M","cookTime":"PT10M","totalTime":"PT15M",
          "recipeIngredient":["200g Synthetic Lentils, rinsed","2 Lime"],"recipeInstructions":[{"@type":"HowToStep","position":1,"text":"Self-authored synthetic instruction."}],
          "aggregateRating":{"ratingValue":"4.2","reviewCount":"100"},"review":[{"text":"DO NOT COPY REVIEWS"}]}
    return '<html><head><link rel="canonical" href="https://www.cherrypick.co/recipes/synthetic-test"><script type="application/ld+json">'+json.dumps(data)+'</script></head><body><nav><span>Gluten Free</span></nav><section><div><div><h1>SYNTHETIC Recipe</h1><span>Vegan</span><span>Star</span></div><ul><li><span>Easy level</span></li></ul></div></section><section id="recipe-reviews-section"><div><p>4.2 out of 5 (100 ratings, 12 with reviews)</p></div></section></body></html>'


class CollectorTests(unittest.TestCase):
    def test_same_origin_and_strict_recipe_urls(self):
        for value in ['https://evil.example/recipes/x','http://www.cherrypick.co/recipes/x','https://user:password@www.cherrypick.co/recipes/x']:
            with self.assertRaises(importer.ImportProblem):collector.safe_url(value)
        for value in ['https://www.cherrypick.co/recipes/categories/vegan','https://www.cherrypick.co/recipes','https://www.cherrypick.co/reviews']:
            with self.assertRaises(importer.ImportProblem):collector.recipe_url(value)
        self.assertEqual(collector.recipe_url('https://www.cherrypick.co/recipes/synthetic-test?slug=synthetic-test'),'https://www.cherrypick.co/recipes/synthetic-test')

    def test_sitemap_xml_safety(self):
        kind,urls=collector.sitemap_links('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://www.cherrypick.co/recipes/synthetic-test</loc></url></urlset>')
        self.assertEqual(kind,'urlset');self.assertEqual(len(urls),1)
        with self.assertRaises(importer.ImportProblem):collector.sitemap_links('<!DOCTYPE foo [<!ENTITY a "bad">]><urlset/>')

    def test_parse_recipe_scope_and_ratings(self):
        with patch('socket.socket',side_effect=AssertionError('No live network in tests')):
            record=collector.parse_public_recipe(synthetic_html(),'https://www.cherrypick.co/recipes/synthetic-test','2026-01-01T00:00:00+00:00','Self-authored synthetic fixture','Self-authored synthetic prose')
        self.assertEqual(record['diets'],['Vegan'])
        self.assertNotIn('Gluten Free',record['categories'])
        self.assertEqual(record['aggregate_rating']['rating_count'],100)
        self.assertEqual(record['aggregate_rating']['review_count'],12)
        self.assertEqual(record['aggregate_rating']['best_rating'],5)
        self.assertEqual(record['difficulty'],'Easy')
        self.assertEqual(record['ingredients'][1]['source_name'],'Lime')
        self.assertIsNone(record['ingredients'][1]['unit'])
        self.assertNotIn('DO NOT COPY REVIEWS',json.dumps(record))

    def test_provider_rating_count_fallback(self):
        html=synthetic_html().replace('recipe-reviews-section','no-review-summary')
        record=collector.parse_public_recipe(html,'https://www.cherrypick.co/recipes/synthetic-test','2026-01-01T00:00:00+00:00','Synthetic','Synthetic')
        self.assertEqual(record['aggregate_rating']['rating_count'],100)
        self.assertIsNone(record['aggregate_rating']['review_count'])
        self.assertIsNone(record['aggregate_rating']['best_rating'])

    def test_challenge_and_canonical_mismatch_stop(self):
        for html in ['<title>Just a moment</title>',synthetic_html().replace('rel="canonical" href="https://www.cherrypick.co/recipes/synthetic-test"','rel="canonical" href="https://www.cherrypick.co/recipes/other-test"')]:
            with self.assertRaises(importer.ImportProblem):collector.parse_public_recipe(html,'https://www.cherrypick.co/recipes/synthetic-test','2026-01-01T00:00:00+00:00','Synthetic','Synthetic')

    def test_ingredient_explicit_amounts_no_container_conversion(self):
        cases=[('200g Synthetic Peas','200','g','Synthetic Peas'),('2 tbsp Synthetic Oil','2','tbsp','Synthetic Oil'),('1 Synthetic Garlic Clove, crushed','1','Clove','Synthetic Garlic'),('0.5 small bunch of Synthetic Herbs','0.5','small bunch','Synthetic Herbs'),('2 Blocks Synthetic Tofu','2','Blocks','Synthetic Tofu'),('Synthetic Salt, to taste',None,None,'Synthetic Salt'),('2 Lime','2',None,'Lime')]
        for original,qty,unit,name in cases:
            with self.subTest(original=original):
                row=collector.parse_ingredient_text(original)
                self.assertEqual((row['quantity_text'],row['unit'],row['source_name']),(qty,unit,name))
                self.assertEqual(row['original_text'],original)
        self.assertEqual(collector.parse_ingredient_text('2 x 400g Synthetic Beans')['canonical_match_status'],'needs_form_review')

    def test_unsafe_rate_refused(self):
        with self.assertRaises(importer.ImportProblem):collector.PublicClient(delay=0)

    def test_robots_cache_expiry_changed_rules_and_fetch_failure(self):
        allowed='User-agent: *\nAllow: /\n'
        denied='User-agent: *\nDisallow: /recipes/\n'
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'robots-cache.json'
            first=collector.PublicClient();first.robots_cache_path=path
            with patch.object(first,'get',return_value=(allowed,collector.ORIGIN+'/robots.txt')) as request,patch('collector.time.time',return_value=10000):
                first.load_robots();self.assertEqual(request.call_count,1)
            second=collector.PublicClient();second.robots_cache_path=path
            with patch.object(second,'get',side_effect=AssertionError('Fresh cache should not fetch')),patch('collector.time.time',return_value=10001):
                second.load_robots();self.assertTrue(second.robot.can_fetch(collector.USER_AGENT,collector.ORIGIN+'/recipes/synthetic-test'))
            expired=collector.PublicClient();expired.robots_cache_path=path
            with patch.object(expired,'get',side_effect=importer.ImportProblem('synthetic_fetch_error')),patch('collector.time.time',return_value=10601):
                with self.assertRaises(importer.ImportProblem):expired.load_robots()
                self.assertIsNone(expired.robot)
            changed=collector.PublicClient();changed.robots_cache_path=path
            with patch.object(changed,'get',return_value=(denied,collector.ORIGIN+'/robots.txt')),patch('collector.time.time',return_value=10602):
                changed.load_robots();self.assertFalse(changed.robot.can_fetch(collector.USER_AGENT,collector.ORIGIN+'/recipes/synthetic-test'))

    def test_robots_cache_different_origin_is_refetched(self):
        text='User-agent: *\nAllow: /\n'
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'robots-cache.json'
            first=collector.PublicClient();first.robots_cache_path=path
            with patch.object(first,'get',return_value=(text,collector.ORIGIN+'/robots.txt')),patch('collector.time.time',return_value=10000):first.load_robots()
            other=collector.PublicClient();other.robots_cache_path=path
            with patch.object(other,'get',return_value=(text,'https://cherrypick.co/robots.txt')) as request,patch('collector.time.time',return_value=10001):
                other.load_robots('https://cherrypick.co');self.assertEqual(request.call_count,1)
                self.assertEqual(other.robot_origin,'https://cherrypick.co')

    def test_robots_html_response_fails_closed(self):
        client=collector.PublicClient()
        with patch.object(client,'get',return_value=('<html><title>Access denied</title></html>',collector.ORIGIN+'/robots.txt')):
            with self.assertRaisesRegex(importer.ImportProblem,'robots_html'):client.load_robots()
        self.assertIsNone(client.robot)

    def test_unicode_and_mixed_fractions(self):
        for value,expected in [('¼',0.25),('½',0.5),('1½',1.5),('1 1/2',1.5),('1/4',0.25)]:
            row=collector.parse_ingredient_text(value+' Synthetic Cabbage')
            self.assertEqual(row['source_name'],'Synthetic Cabbage')
            self.assertEqual(importer.explicit_quantity(row['quantity_text']),expected)
        self.assertIsNone(importer.explicit_quantity('2-3'))

    def test_author_labels_not_categories(self):
        html=synthetic_html().replace('<span>Vegan</span>','<span>Vegan</span><span>by Synthetic Author</span><span>Synthetic Author</span><span>Cherrypick</span>')
        parser=collector.RecipeHTMLParser();parser.feed(html)
        self.assertEqual(collector.page_metadata(parser)['categories'],['Vegan'])

    def test_collection_resume_reuses_hash_checked_cache(self):
        first='https://www.cherrypick.co/recipes/synthetic-test'
        second='https://www.cherrypick.co/recipes/synthetic-test-two'
        class FakeClient:
            delay=2.0
            requests=0
            calls=[]
            def load_robots(self):return ['https://www.cherrypick.co/sitemap.xml'],'0'*64
            def get(self,url):
                self.requests+=1;self.calls.append(url)
                if url.endswith('sitemap.xml'):
                    return '<urlset><url><loc>'+first+'</loc></url><url><loc>'+second+'</loc></url></urlset>',url
                return synthetic_html().replace('synthetic-test',url.rsplit('/',1)[1]),url
        client=FakeClient()
        with tempfile.TemporaryDirectory() as temp,patch.object(collector,'PublicClient',return_value=client),redirect_stdout(io.StringIO()):
            args=Namespace(output=Path(temp)/'collection',limit=1,resume=False,urls_file=None,rights_reference='Synthetic test collection',text_rights_reference='Self-authored synthetic prose',delay_seconds=2.0,retries=0)
            self.assertEqual(collector.collect(args),0)
            args.resume=True;args.limit=2
            self.assertEqual(collector.collect(args),0)
            self.assertEqual(client.calls.count(first),1)
            self.assertEqual(client.calls.count(second),1)
            record=next((args.output/'records').glob('*.json'))
            data=json.loads(record.read_text());data['record']['title']='SYNTHETIC tamper';record.write_text(json.dumps(data))
            self.assertEqual(collector.collect(args),2)
            self.assertEqual(client.calls.count(first),1)
            self.assertEqual(client.calls.count(second),1)

    def test_compact_checkpoint_can_be_exported_later(self):
        url='https://www.cherrypick.co/recipes/synthetic-test'
        class FakeClient:
            delay=2.0
            requests=0
            def load_robots(self):return [],'0'*64
            def get(self,u):self.requests+=1;return synthetic_html(),u
        client=FakeClient()
        with tempfile.TemporaryDirectory() as temp,patch.object(collector,'PublicClient',return_value=client),patch.object(collector,'discover',return_value=([url],[])),redirect_stdout(io.StringIO()):
            args=Namespace(output=Path(temp)/'collection',limit=1,resume=False,urls_file=None,rights_reference='Synthetic collection',text_rights_reference='Synthetic prose',delay_seconds=2.0,retries=0,checkpoint_only=True)
            self.assertEqual(collector.collect(args),0)
            snap=next((args.output/'snapshots').iterdir())
            self.assertEqual(json.loads((snap/'collection-manifest.json').read_text())['snapshot_type'],'checkpoint_only')
            self.assertFalse((snap/'normalized').exists())
            args.resume=True;args.checkpoint_only=False
            self.assertEqual(collector.collect(args),0)
            self.assertEqual(client.requests,1)
            self.assertEqual(len(list((args.output/'snapshots').glob('*/normalized/catalogue.json'))),1)


if __name__=='__main__':unittest.main()
