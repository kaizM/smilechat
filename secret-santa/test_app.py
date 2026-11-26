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
                    request(client,'save',wish={'choices':['0:Other','2:Blue'],'other':{'0':f'Gift {i}'},'custom':f'Custom {i}','sizes':'M','avoid':'Peanuts','links':'https://example.com/gift'})
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
                for result in results:
                    recipient = result['recipient']
                    idx = int(recipient['name'].split()[-1])
                    self.assertEqual(recipient['wish'], {'choices':['0:Other','2:Blue'],'other':{'0':f'Gift {idx}'},'custom':f'Custom {idx}','sizes':'M','avoid':'Peanuts','links':'https://example.com/gift','completed':True})
                first_recipient = first['recipient']
                recipient_idx = int(first_recipient['name'].split()[-1])
                request(clients[recipient_idx],'save',wish={'choices':['0:Books'],'other':{},'custom':'Updated preference'})
                refreshed = request(clients[0],'status')['me']
                self.assertEqual(refreshed['drawNumber'],first['drawNumber'])
                self.assertEqual(refreshed['recipient']['name'],first_recipient['name'])
                self.assertEqual(refreshed['recipient']['wish']['custom'],'Updated preference')
                request(clients[recipient_idx],'save',wish=first_recipient['wish'])
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
                self.assertEqual(clients[0].post('/api',json={'action':'resetEvent','confirmation':'RESET EVENT'}).status_code,403)
                self.assertEqual(admin.post('/api',json={'action':'resetEvent','confirmation':'wrong'}).status_code,400)
                reset_draw=request(admin,'resetDraw',confirmation='RESET DRAW')
                self.assertFalse(reset_draw['active'])
                self.assertEqual(len(reset_draw['participants']),14)
                reopened=request(clients[0],'status')['me']
                self.assertNotIn('recipient',reopened)
                self.assertTrue(reopened['wish']['completed'])
                self.assertTrue(request(admin,'activate')['active'])
                reset_event=request(admin,'resetEvent',confirmation='RESET EVENT')
                self.assertFalse(reset_event['active'])
                self.assertEqual(reset_event['participants'],[])
                self.assertTrue(reset_event['admin'])
                self.assertEqual(request(clients[0],'status'),{'active':False,'admin':False})
                new_account=request(clients[0],'register',name='New Employee',username='emp00',pin='0123')
                self.assertFalse(new_account['me']['wish']['completed'])
                self.assertEqual(len(request(admin,'status')['participants']),1)
            for client in clients: client.close()

if __name__=='__main__': unittest.main()
