import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from control import Control, Handler

class HTTPTests(unittest.TestCase):
    def test_authenticated_admin_and_public_discovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=tmp+'/control.db'
            c=Control(path); c.bootstrap('admin@example.test','long-test-password'); c.db.close()
            server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.db_path=path
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            base='http://127.0.0.1:'+str(server.server_port)
            def call(route,data=None,token='',key=''):
                req=Request(base+route,data=json.dumps(data).encode() if data else None,headers={'Content-Type':'application/json','Authorization':'Bearer '+token,'X-API-Key':key})
                with urlopen(req) as response:
                    self.assertEqual(response.headers['Cache-Control'],'no-store')
                    return json.load(response)
            try:
                self.assertEqual(call('/api/stores'),[])
                with self.assertRaises(HTTPError) as error:call('/api/admin')
                self.assertEqual(error.exception.code,403)
                token=call('/api/login',dict(email='admin@example.test',password='long-test-password'))['token']
                self.assertEqual(call('/api/admin',token=token)['me']['email'],'admin@example.test')
                call('/api/admin/tenants',dict(name='Global merchant'),token)
                self.assertEqual(len(call('/api/admin',token=token)['tenants']),1)
                demo=call('/api/demo/login',dict(email='demo@globalpos.example',password='GlobalPOS-Demo!2026'))
                prefix='/api/demo/'+demo['demo_id']
                demo_state=call(prefix+'/admin',token=demo['token'])
                self.assertEqual(len(demo_state['stores']),2)
                self.assertEqual(demo_state['me']['super'],1)
                call(prefix+'/admin/tenants',dict(name='Only demo'),demo['token'])
                self.assertEqual(len(call('/api/admin',token=token)['tenants']),1)
                self.assertEqual(len(call(prefix+'/stores')),2)
                market=call(prefix+'/marketplace')
                store=market['stores'][0]
                issued=call(prefix+'/admin/api_keys',dict(store_id=store['id'],name='HTTP test key',scopes='inventory:read'),demo['token'])
                inventory=call(prefix+'/integration/inventory?store_id='+store['id'],key=issued['secret'])
                self.assertEqual(len(inventory),2)
                with self.assertRaises(HTTPError) as denied:
                    call(prefix+'/integration/inventory',dict(store_id=store['id'],name='No write',barcode='NO',quantity=1,price_minor=1),key=issued['secret'])
                self.assertEqual(denied.exception.code,403)
                checkout_key='a'*64
                order=call(prefix+'/checkout',dict(store_id=store['id'],gateway_id=store['gateways'][0]['id'],email='sample@example.test',checkout_key=checkout_key,items=[dict(id=store['products'][0]['id'],quantity=1)],accepted=True,terms_version=market['terms_version'],terms_digest=market['terms_digest']))
                self.assertEqual(order['status'],'awaiting_demo')
                verified=call(prefix+'/order/verify',dict(id=order['id'],checkout_key=checkout_key,simulate='pay'))
                self.assertEqual(verified['status'],'demo_paid')
                call(prefix+'/admin/api_keys',dict(id=issued['id'],store_id=store['id'],action='revoke'),demo['token'])
                with self.assertRaises(HTTPError) as revoked:call(prefix+'/integration/inventory?store_id='+store['id'],key=issued['secret'])
                self.assertEqual(revoked.exception.code,403)
                with self.assertRaises(HTTPError) as rejected:call('/api/admin',token=demo['token'])
                self.assertEqual(rejected.exception.code,403)
            finally:
                server.shutdown();server.server_close();thread.join()
