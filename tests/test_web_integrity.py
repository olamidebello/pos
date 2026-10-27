import json
import re
import struct
import unittest
from html.parser import HTMLParser
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
class Parser(HTMLParser):
    def __init__(self):super().__init__();self.ids=set();self.links=[]
    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs)
        if attrs.get('id'):self.ids.add(attrs['id'])
        for key in ('href','src'):
            if attrs.get(key,'').startswith('/'):self.links.append(attrs[key])

class WebIntegrityTests(unittest.TestCase):
    def test_all_html_links_and_assets_exist_in_deployed_layout(self):
        for path in (ROOT/'web').rglob('*.html'):
            parsed=Parser();parsed.feed(path.read_text())
            for value in parsed.links:
                route=value.split('?')[0].split('#')[0]
                relative=route.lstrip('/') or 'index.html'
                target=ROOT/relative if route.startswith('/agreements/') else ROOT/'web'/relative
                self.assertTrue(target.exists(),str(path)+': '+value)
    def test_admin_and_marketplace_script_elements_exist(self):
        for page,script,accessor in [('admin.html','admin.js','el'),('marketplace.html','marketplace.js','m')]:
            parsed=Parser();parsed.feed((ROOT/'web'/page).read_text())
            used=set(re.findall(r"\b"+accessor+r"\('([^']+)'\)",(ROOT/'web'/script).read_text()))
            self.assertEqual(used-parsed.ids,set())
    def test_manifest_icons_have_correct_dimensions(self):
        manifest=json.loads((ROOT/'web/manifest.webmanifest').read_text())
        self.assertEqual(manifest['name'],'Global POS')
        for icon in manifest['icons']:
            png=(ROOT/'web'/icon['src'].lstrip('/')).read_bytes()
            width,height=struct.unpack('>II',png[16:24])
            self.assertEqual(str(width)+'x'+str(height),icon['sizes'])
