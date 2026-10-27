import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import json
from control import Control
from product_search import search
import discovery

class ProductSearchTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=str(Path(self.temp.name)/'db')
        self.c=Control(self.path);self.c.bootstrap('admin@example.test','private-long-password')
        self.actor=self.c.actor(self.c.login('admin@example.test','private-long-password'))
        tenant=self.c.mutate(self.actor,'tenants',{'name':'Seller'})['id']
        self.store=self.c.mutate(self.actor,'stores',dict(tenant_id=tenant,name='Water seller',address='High Street',country='NG',currency='NGN',timezone='UTC',language='en',lat=6,lon=3))['id']
        self.c.mutate(self.actor,'inventory',dict(store_id=self.store,barcode='123',name='Bottled drinking water',quantity=8,price_minor=50000))
    def tearDown(self):self.c.db.close();self.temp.cleanup()
    def data(self,**kwargs):return dict(query='drinking water',lat=6,lon=3,**kwargs)
    def test_description_stock_price_and_distance(self):
        result=search(self.data(),self.path)['stores'][0]
        self.assertEqual(result['distance_km'],0)
        self.assertEqual(result['products'][0]['price_minor'],50000)
        self.assertEqual(result['products'][0]['quantity'],8)
    def test_far_store_and_suspended_store_excluded(self):
        self.assertEqual(search(dict(self.data(),lat=20),self.path)['stores'],[])
        self.c.db.execute("UPDATE stores SET status='suspended'");self.c.db.commit()
        self.assertEqual(search(self.data(),self.path)['stores'],[])
    def test_barcode_and_literal_wildcards(self):
        self.assertEqual(len(search(dict(self.data(),query='123'),self.path)['stores']),1)
        self.assertEqual(search(dict(self.data(),query='%%'),self.path)['stores'],[])
        self.assertEqual(search(dict(self.data(),query="'; DROP TABLE inventory; --"),self.path)['stores'],[])
        self.assertEqual(self.c.db.execute('SELECT COUNT(*) FROM inventory').fetchone()[0],1)
    def test_unsafe_website_links_rejected(self):
        for value in ('javascript:alert(1)','https://localhost/x','https://127.0.0.1','https://user:password@example.com','http://shop.com','https://shop.com:8787'):
            self.assertEqual(discovery.website_url(value),'')
        self.assertEqual(discovery.website_url('https://store.example.org/shop'),'https://store.example.org/shop')
    def test_query_is_data_never_ansible_template(self):
        discovery.CACHE.clear();query="{{ lookup('pipe', 'id') }}"
        def runner(args,**kwargs):
            variables=json.loads(Path(args[-1][1:]).read_text())
            self.assertNotIn('query',json.dumps(variables))
            self.assertEqual(json.loads(Path(variables['discovery_input']).read_text())['query'],query)
            Path(variables['discovery_output']).write_text('{"findings":[]}')
            return type('Result',(),{'returncode':0})()
        with patch('discovery.subprocess.run',side_effect=runner):discovery.discover(dict(self.data(),query=query),self.path)
