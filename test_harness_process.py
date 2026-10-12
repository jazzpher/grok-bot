"""Hard process death/restart test using a dummy local HTTP provider."""
import json
import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests


class ProcessLifecycleTests(unittest.TestCase):
    def test_killed_server_restores_cookie_task_and_resumes_same_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            root=pathlib.Path(directory)
            entered=threading.Event(); release=threading.Event(); phase={'initial':True,'n':0}
            class Handler(BaseHTTPRequestHandler):
                def log_message(self,*a): pass
                def do_POST(self):
                    data=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                    self.assert_payload=data
                    if phase['initial']:
                        entered.set(); release.wait(10)
                    actions=[{'action':'write_file','path':'out.txt','content':'dummy'},
                             {'action':'read_file','path':'out.txt'}, {'action':'done','text':'verified'}]
                    a=actions[min(phase['n'],2)]; phase['n']+=1
                    payload=json.dumps({'choices':[{'message':{'content':json.dumps(a)}}]}).encode()
                    self.send_response(200); self.send_header('Content-Length',str(len(payload))); self.end_headers()
                    try: self.wfile.write(payload)
                    except (BrokenPipeError,ConnectionResetError): pass
            provider=ThreadingHTTPServer(('127.0.0.1',0),Handler)
            pt=threading.Thread(target=provider.serve_forever,daemon=True); pt.start()
            with socket.socket() as s:
                s.bind(('127.0.0.1',0)); port=s.getsockname()[1]
            env=dict(os.environ,GROK_WORKSPACE_DIR=str(root/'workspaces'),GROK_TASK_DB=str(root/'tasks.db'),
                     GROK_SESSION_SECRET='dummy-stable-session-secret-for-tests',GROK_API_KEY='DUMMY_NOT_REAL',
                     GROK_API_URL='http://127.0.0.1:'+str(provider.server_port)+'/v1/chat/completions',
                     GROK_APP_PASSWORD='',GROK_DEPLOYMENT_MODE='local')
            process=None
            def start():
                p=subprocess.Popen([sys.executable,'webui.py','--port',str(port)],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                for _ in range(200):
                    if p.poll() is not None: self.fail('server exited')
                    try:
                        if requests.get('http://127.0.0.1:'+str(port)+'/',timeout=.2).status_code==200: return p
                    except requests.RequestException: pass
                    time.sleep(.02)
                p.kill(); p.wait(); self.fail('server not ready')
            client=requests.Session(); url='http://127.0.0.1:'+str(port)
            try:
                process=start()
                client.get(url+'/')
                response=client.post(url+'/task',json={'message':'Write dummy to out.txt',
                                      'checks':[{'type':'file_text','path':'out.txt','text':'dummy'}]})
                self.assertEqual(response.status_code,202); tid=response.json()['id']
                self.assertTrue(entered.wait(3))
                process.kill(); process.wait(3)
                phase['initial']=False; release.set(); phase['n']=0
                # Wait for the dead client's provider request to finish before next calls.
                time.sleep(.1); phase['n']=0
                process=start()
                row=client.get(url+'/tasks/'+tid).json()
                self.assertEqual(row['status'],'interrupted')
                self.assertEqual(row['steps'],1)
                self.assertEqual(client.post(url+'/tasks/'+tid+'/resume',json={}).status_code,202)
                for _ in range(300):
                    row=client.get(url+'/tasks/'+tid).json()
                    if row['status'] not in ('running','queued'): break
                    time.sleep(.02)
                self.assertEqual(row['status'],'completed')
                self.assertEqual(row['steps'],4)
                self.assertEqual(client.get(url+'/files/out.txt').content,b'dummy')
                self.assertNotIn('DUMMY_NOT_REAL',(root/'tasks.db').read_bytes().decode('latin1'))
            finally:
                if process and process.poll() is None: process.kill(); process.wait(3)
                release.set(); client.close(); provider.shutdown(); provider.server_close(); pt.join(2)
