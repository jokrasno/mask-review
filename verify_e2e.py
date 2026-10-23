"""Generate geometric data and exercise a real local server. Never contacts Google."""
import argparse
import base64
from email.parser import BytesParser
from email.policy import default
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import parse_qs, urlencode, urlparse
import urllib.error
import urllib.request
import uuid
import zipfile

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent
CLASSES = {'0':'background','1':'barrier','2':'bollard','3':'person','4':'industrial_vehicle'}
CONFIG = {'format':'mask-review-library-config-v1','folders':{key:'fixture_'+key+'_folder' for key in ('library','originals','labels','incoming')}}


def fixture_zip():
    image = Image.new('RGB', (40, 24), '#fafafa')
    draw = ImageDraw.Draw(image)
    draw.rectangle((3, 3, 17, 17), fill='#3174ad')
    draw.ellipse((23, 5, 35, 17), fill='#da9a25')
    labels = Image.new('L', image.size)
    draw = ImageDraw.Draw(labels)
    for cid in range(1, 5):
        draw.rectangle((cid*7, 4, cid*7+4, 18), fill=cid)
    photo, mask, package = io.BytesIO(), io.BytesIO(), io.BytesIO()
    image.save(photo, format='PNG'); labels.save(mask, format='PNG')
    manifest = dict(format='mask-review-v1', taxonomy='v6-object-5', classes=CLASSES,
                    title='Generated geometric review', samples=[])
    with zipfile.ZipFile(package, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('photo.png', photo.getvalue()); archive.writestr('suggestion.png', mask.getvalue())
        for index in range(2):
            manifest['samples'].append(dict(id='geometric-'+str(index), image='photo.png', suggestion='suggestion.png',
                filename='geometric.png', model='generated geometric fixture', dataset='generated', session='session-1',
                run='run-1', split='development', state='approved', reviewer='Imported approval must not transfer'))
        archive.writestr('manifest.json', json.dumps(manifest))
    return package.getvalue(), labels.tobytes()


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


class FakeDrive(BaseHTTPRequestHandler):
    """Bounded local fixture transport for OAuth PKCE and immutable uploads."""
    def log_message(self, *args):
        pass

    def reply(self, status, body, headers=None):
        raw = json.dumps(body).encode()
        self.send_response(status); self.send_header('Content-Length', str(len(raw)))
        self.send_header('Content-Type', 'application/json')
        for key, value in (headers or {}).items(): self.send_header(key, value)
        self.end_headers(); self.wfile.write(raw)

    def do_GET(self):
        self.server.calls.append(self.path.split('?')[0])
        parsed = urlparse(self.path); query = parse_qs(parsed.query); path = parsed.path
        if path == '/authorize':
            assert query['code_challenge_method'] == ['S256']
            self.server.challenge = query['code_challenge'][0]
            self.server.redirect = query['redirect_uri'][0]
            return self.reply(302, {}, {'Location':self.server.redirect+'?'+urlencode({'code':'fixture-code','state':query['state'][0]})})
        if self.headers.get('Authorization') != 'Bearer fixture-access': return self.reply(401,{})
        if path == '/drive/v3/about': return self.reply(200, {'user':{'displayName':'Fixture Reviewer','emailAddress':'reviewer@example.invalid','permissionId':'fixture-reviewer'}})
        if path == '/drive/v3/files/generateIds': return self.reply(200, {'ids':['fixture-'+uuid.uuid4().hex]})
        if path == '/drive/v3/files': return self.reply(200, {'files':[]})
        fid = path.rsplit('/',1)[-1]
        if fid in self.server.files: return self.reply(200,self.server.files[fid])
        return self.reply(404,{})

    def do_POST(self):
        path = urlparse(self.path).path; self.server.calls.append(path)
        raw = self.rfile.read(int(self.headers['Content-Length']))
        if path == '/token':
            params = parse_qs(raw.decode())
            challenge = base64.urlsafe_b64encode(hashlib.sha256(params['code_verifier'][0].encode()).digest()).decode().rstrip('=')
            assert challenge == self.server.challenge and params['redirect_uri'][0] == self.server.redirect
            return self.reply(200, dict(access_token='fixture-access', refresh_token='fixture-refresh', expires_in=3600, scope='https://www.googleapis.com/auth/drive'))
        if path == '/upload/drive/v3/files':
            msg = BytesParser(policy=default).parsebytes(('Content-Type: '+self.headers['Content-Type']+'\r\nMIME-Version: 1.0\r\n\r\n').encode()+raw)
            parts = list(msg.iter_parts()); meta = json.loads(parts[0].get_payload(decode=True)); blob = parts[1].get_payload(decode=True)
            assert meta['parents'] == [CONFIG['folders']['labels']]
            meta.update(size=str(len(blob)), md5Checksum=hashlib.md5(blob).hexdigest())
            self.server.files[meta['id']] = meta
            return self.reply(200, meta)
        return self.reply(404,{})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, help='New evidence folder outside the source repository; defaults to a temporary folder')
    parser.add_argument('--google-fixture', action='store_true', help='Also exercise loopback OAuth/Drive with native OS credential storage')
    args = parser.parse_args()
    out = args.output.resolve() if args.output else Path(tempfile.mkdtemp(prefix='mask-review-e2e-'))
    if out == ROOT or ROOT in out.parents: parser.error('Evidence must stay outside the source repository')
    if args.output:
        out.mkdir(parents=True, exist_ok=False)
    checks = []
    fixture, expected = fixture_zip(); (out/'generated-review.zip').write_bytes(fixture)
    fake = ThreadingHTTPServer(('127.0.0.1',0), FakeDrive)
    fake.calls=[]; fake.files={}
    threading.Thread(target=fake.serve_forever,daemon=True).start()
    origin = 'http://127.0.0.1:'+str(free_port())
    log = (out/'server.log').open('ab'); process=None

    def call(path, body=None, auth=True, expected_status=200, extra=None):
        headers=dict(extra or {})
        if auth and body is not None: headers['X-Review-Token']=call('/api/state',auth=False)['token']
        if body is not None:
            headers['Content-Type']='application/zip' if isinstance(body,bytes) else 'application/json'
            if not isinstance(body,bytes): body=json.dumps(body).encode()
        try:
            with urllib.request.urlopen(urllib.request.Request(origin+path,body,headers),timeout=10) as response:
                raw=response.read(); status=response.status; kind=response.headers.get('Content-Type','')
        except urllib.error.HTTPError as error:
            status=error.code; raw=error.read(); kind='application/json'
        assert status==expected_status,(path,status,raw[:200])
        return json.loads(raw) if 'application/json' in kind else raw

    def start():
        nonlocal process
        process=subprocess.Popen([sys.executable,str(ROOT/'desktop_server.py'),'--port',origin.rsplit(':',1)[-1],
            '--data',str(out/'data'),'--private',str(out/'private'),'--fixture-service',f'http://127.0.0.1:{fake.server_port}','--interval','2'],stdout=log,stderr=log)
        last_error = None
        for _ in range(100):
            if process.poll() is not None: raise AssertionError('Server failed; inspect server.log')
            try:
                call('/api/state'); return
            except OSError as error:
                last_error = error
                time.sleep(.1)
        raise AssertionError('Server did not become ready: '+str(last_error))

    def passed(name):
        checks.append(name); print('PASS '+name,flush=True)

    try:
        start()
        assert not call('/api/drive/status')['library_configured']
        call('/api/drive/connect',{'acknowledge_scope':True},expected_status=400)
        call('/api/drive/library',expected_status=400)
        call('/api/drive/library-config',{'library':CONFIG},auth=False,expected_status=403)
        call('/api/drive/library-config',{'library':CONFIG},extra={'Origin':'https://example.invalid'},expected_status=403)
        passed('unconfigured network access and unauthorized mutation blocked')
        state=call('/api/import',fixture); bid=state['active_batch']
        state=call('/api/state'); assert all(s['state']=='pending' and not s['reviewer'] for s in state['batch']['samples'])
        assert Image.open(io.BytesIO(call(f'/asset/{bid}/0/working'))).tobytes()==expected
        call('/api/export',{'batch':bid},expected_status=400)
        call('/api/decision',dict(batch=bid,index=0,revision=0,decision='approved',reviewer=''),expected_status=400)
        passed('imported suggestions stay pending, native IDs exact, unnamed approval and export blocked')
        edited=bytearray(expected); edited[0]=4
        sample=call('/api/mask',dict(batch=bid,index=0,revision=0,pixels=base64.b64encode(edited).decode(),reason='brush'))
        call('/api/decision',dict(batch=bid,index=0,revision=sample['revision'],decision='approved',reviewer='Fixture Reviewer'))
        exported=call('/api/export',{'batch':bid}); (out/'approved-export.zip').write_bytes(exported)
        archive=zipfile.ZipFile(io.BytesIO(exported)); manifest=json.loads(archive.read('manifest.json'))
        assert len(manifest['samples'])==1 and len(manifest['decisions'])==2
        assert Image.open(io.BytesIO(archive.read(manifest['samples'][0]['mask']))).tobytes()==bytes(edited)
        assert not fake.calls
        passed('offline edit, named approval and approved-only export preserve native bytes without network')
        process.terminate();process.wait(timeout=10);start()
        state=call('/api/state'); assert state['batch']['samples'][0]['state']=='approved'
        assert len(state['batch']['samples'][0]['history'])==2
        assert Image.open(io.BytesIO(call(f'/asset/{bid}/0/working'))).tobytes()==bytes(edited)
        assert call('/api/drive/status')['queued']>=2 and not fake.calls
        passed('restart preserves approval, mask history and offline queue')
        for invalid in ({},dict(CONFIG,unexpected=True),dict(CONFIG,folders=dict.fromkeys(CONFIG['folders'],'duplicate_folder')),json.loads((ROOT/'library-config.example.json').read_text())):
            call('/api/drive/library-config',{'library':invalid},expected_status=400)
        call('/api/drive/library-config',{'library':CONFIG})
        assert json.loads((out/'private'/'library-config.json').read_text())==CONFIG
        changed=json.loads(json.dumps(CONFIG));changed['folders']['labels']='another_labels_folder'
        call('/api/drive/library-config',{'library':changed},expected_status=400)
        assert json.loads((out/'private'/'library-config.json').read_text())==CONFIG
        assert not fake.calls
        passed('invalid configs and library switching rejected, valid config saved privately without network')
        if args.google_fixture:
            call('/api/drive/configure',{'client':{'installed':{'client_id':'fixture.apps.googleusercontent.com','client_secret':'fixture-only'}}})
            auth=call('/api/drive/connect',{'acknowledge_scope':True})
            with urllib.request.urlopen(auth['url'],timeout=10) as response: assert b'Google connected' in response.read()
            for _ in range(100):
                status=call('/api/drive/status')
                if status['last_sync'] and not status['queued'] and not status['activity']: break
                time.sleep(.1)
            assert not status['queued'] and not status['error'],status
            assert len(fake.files)>=2
            call('/api/drive/disconnect',{})
            passed('loopback OAuth PKCE, native credential persistence and immutable queued uploads, then disconnect')
        call('/api/sam/generate',{},expected_status=400)
        assert b'No model worker is bundled' in call('/app.js')
        assert b'drive-library-config' in call('/desktop.js')
        passed('model worker disabled and public UI assets include private config import')
        receipt=dict(result='PASS',checks=checks,google_live_verified=False,fixture_calls=fake.calls,
            command=[sys.executable,str(Path(__file__).resolve()),'--output',str(out)]+(['--google-fixture'] if args.google_fixture else []),
            source_sha256={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in ROOT.iterdir() if p.is_file()},
            export_sha256=hashlib.sha256(exported).hexdigest())
        (out/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n',encoding='utf-8')
        print('Evidence: '+str(out),flush=True)
    finally:
        if process and process.poll() is None:process.terminate();process.wait(timeout=10)
        fake.shutdown();fake.server_close();log.close()


if __name__=='__main__':
    main()
