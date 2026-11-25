from __future__ import annotations
import os, tempfile, unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from fastapi.testclient import TestClient

class DrawTest(unittest.TestCase):
    def test_full_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            os.environ['ADMIN_PASSWORD']='test-only-organizer'
            import app as server
            server.LOCAL=str(Path(folder)/'test.sqlite3')
            server.initialize()
            clients=[TestClient(server.app) for _ in range(14)]
            def request(client: TestClient, action: str, **data: object) -> dict:
                response=client.post('/api',json={'action':action,**data})
                self.assertEqual(response.status_code,200,response.text)
                return response.json()
            with TestClient(server.app) as admin:
                request(admin,'adminLogin',username='admin',password='test-only-organizer')
                for i,client in enumerate(clients):
                    reg=request(client,'register',name=f'Employee {i}',username=f'emp{i:02}',pin='0123')
                    self.assertFalse(reg['me']['wish']['completed'])
                    self.assertNotIn('recipient',reg['me'])
                    request(client,'save',wish={'choices':['0:Other'],'other':{'0':f'Gift {i}'}})
                    self.assertEqual(client.post('/api',json={'action':'reveal'}).status_code,403)
                    self.assertEqual(client.post('/api',json={'action':'activate'}).status_code,403)
                self.assertEqual(admin.post('/api',json={'action':'activate'}).status_code,400)
                for i in range(14): request(admin,'approve',target=f'emp{i:02}')
                self.assertTrue(request(admin,'activate')['active'])
                self.assertEqual(request(clients[0],'status')['availableNumbers'],list(range(1,15)))
                first=request(clients[0],'reveal')['me']
                self.assertNotIn(first['drawNumber'],request(clients[1],'status')['availableNumbers'])
                with ThreadPoolExecutor(max_workers=8) as pool:
                    results=[first,*list(pool.map(lambda c:request(c,'reveal')['me'],clients[1:]))]
                self.assertEqual({r['drawNumber'] for r in results},set(range(1,15)))
                self.assertEqual(len({r['recipient']['name'] for r in results}),14)
                self.assertTrue(all(r['name']!=r['recipient']['name'] for r in results))
                self.assertEqual(request(clients[0],'reveal')['me']['drawNumber'],first['drawNumber'])
                request(clients[0],'logout')
                signed=request(clients[0],'login',username='emp00',pin='0123')['me']
                self.assertEqual(signed['recipient'],first['recipient'])
                self.assertEqual(signed['drawNumber'],first['drawNumber'])
                self.assertEqual(request(clients[0],'status')['availableNumbers'],[])
                self.assertEqual(TestClient(server.app).post('/api',json={'action':'status'}).json(),{'active':True,'admin':False})
                self.assertTrue(all('recipient' not in p and 'drawNumber' not in p for p in request(admin,'status')['participants']))
                self.assertEqual(admin.post('/api',json={'action':'activate'}).status_code,400)
                self.assertEqual(TestClient(server.app).post('/api',json={'action':'register','name':'Late','username':'late','pin':'1234'}).status_code,400)
            for client in clients: client.close()

if __name__=='__main__': unittest.main()
