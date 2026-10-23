"""Loopback desktop editor with an independent local cache and Drive outbox."""
import argparse
import base64
import json
import os
import socket
from pathlib import Path
import threading
from urllib.parse import urlparse, parse_qs
import webbrowser
import server as editor
from drive_sync import DriveSync
from desktop_platform import default_paths

SYNC=None
ORIGINAL_EXPORT=editor.export_batch


def checked_export(bid):
    with editor.LOCK,SYNC.guard:
        batch=editor.read_batch(bid)
        for update in SYNC.updates():
            if update['batch']==bid and update['conflict'] and batch['samples'][update['index']]['state']=='approved':
                raise ValueError('Reconcile conflicting approved frames before exporting this batch')
        if any(sample['state']=='approved' and SYNC.unresolved_source(sample['image_sha256']) for sample in batch['samples']):
            raise ValueError('Choose the capture metadata for conflicting photo assignments before exporting')
        if any(sample['state']=='approved' and not SYNC.selected_source(sample) for sample in batch['samples']):
            raise ValueError('This approved frame uses a non-selected photo assignment; open the selected assignment and review it again')
        return ORIGINAL_EXPORT(bid)


class DesktopHandler(editor.Handler):
    def log_message(self, fmt, *args):
        pass  # OAuth callback codes and private paths must never enter logs.

    def send(self,status,body,kind='application/json',extra=None):
        if self.command=='POST' and status==200 and self.path in ('/api/mask','/api/note','/api/decision','/api/undo_review','/api/import'):
            # Same editor lock as local mutation. Startup scan recovers interruption.
            SYNC.scan_local()
        return super().send(status,body,kind,extra)

    def do_GET(self):
        path=urlparse(self.path).path
        query=parse_qs(urlparse(self.path).query)
        if path=='/' and any(key in query for key in ('state','code','error')):
            path='/oauth/callback'
        if path in ('/','/desktop.js','/desktop.css') and self.host_ok():
            if path=='/':
                raw=(editor.ROOT/'index.html').read_text('utf-8').replace('</head>','<link rel="stylesheet" href="/desktop.css"><script src="/desktop.js" defer></script></head>')
                return self.send(200,raw.encode(),'text/html; charset=utf-8')
            return self.send(200,(editor.ROOT/path[1:]).read_bytes(),'text/javascript' if path.endswith('.js') else 'text/css')
        if not path.startswith(('/api/drive/','/oauth/')):
            return super().do_GET()
        if not self.host_ok():
            return self.send(403,{'error':'Loopback address required'})
        try:
            if path=='/oauth/callback':
                SYNC.drive.exchange(query);SYNC.paused=False;SYNC.j['paused']=False;SYNC.persist();SYNC.wake.set()
                return self.send(200,b'<!doctype html><meta charset="utf-8"><title>Google connected</title><h1>Google connected</h1><p><a href="/">Return to Mask Review</a>, or close this sign-in tab.</p>','text/html; charset=utf-8')
            if path=='/api/drive/status':
                return self.send(200,SYNC.status())
            if path=='/api/drive/library':
                return self.send(200,{'files':SYNC.library_files()})
            if path.startswith('/api/drive/preview/'):
                rid=path.rsplit('/',1)[1]
                if rid not in SYNC.j['revisions']:
                    raise ValueError('Unknown revision')
                import zipfile
                raw=zipfile.ZipFile(SYNC.root/'packages'/f'{rid}.zip').read('mask.png')
                return self.send(200,raw,'image/png')
            if path=='/api/drive/client':
                return self.send(200,{'configured':SYNC.drive.client_path.exists()})
            return self.send(404,{'error':'Not found'})
        except Exception as error:
            # No raw OAuth/server response can escape through an exception.
            text=str(error)[:180] if isinstance(error,ValueError) else 'Drive operation failed. Local work is retained.'
            return self.send(400,{'error':text})

    def do_POST(self):
        if not self.path.startswith('/api/drive/'):
            # Assign authenticated Google name to decisions, preserving local offline
            # work if disconnected. Local reviewer names remain explicit otherwise.
            return super().do_POST()
        origin=self.headers.get('Origin')
        allowed=(f'http://127.0.0.1:{self.server.server_port}',f'http://localhost:{self.server.server_port}')
        if not self.host_ok() or (origin and origin not in allowed) or self.headers.get('Sec-Fetch-Site')=='cross-site' or self.headers.get('X-Review-Token')!=editor.TOKEN:
            return self.send(403,{'error':'Local session authorization required'})
        try:
            length=int(self.headers.get('Content-Length','0'))
            if not 0<length<=18*1024*1024:
                raise ValueError('Request exceeds size limit')
            body=json.loads(self.rfile.read(length))
            path=self.path
            if path=='/api/drive/library-config':
                result=SYNC.configure_library(body['library'])
            elif path=='/api/drive/configure':
                SYNC.drive.configure(body['client']);result={'configured':True}
            elif path=='/api/drive/connect':
                SYNC.require_library()
                if body.get('acknowledge_scope') is not True:
                    raise ValueError('Acknowledge existing-folder Drive access before signing in')
                result={'url':SYNC.drive.begin(self.server.server_port)}
            elif path=='/api/drive/disconnect':
                SYNC.paused=True;SYNC.j['paused']=True;SYNC.persist();SYNC.drive.disconnect();result={'disconnected':True}
            elif path=='/api/drive/sync':
                SYNC.paused=bool(body.get('paused',False));SYNC.j['paused']=SYNC.paused;SYNC.persist();SYNC.wake.set();result={'queued':True}
            elif path=='/api/drive/import':
                result=SYNC.import_library(body['file_id']);SYNC.wake.set()
            elif path=='/api/drive/reconcile':
                result=SYNC.reconcile(body['frame'],body['selected'],body['heads'],body['reviewer'])
            elif path=='/api/drive/incoming':
                raw=base64.b64decode(body['bytes'],validate=True)
                result={'id':SYNC.quarantine_photo(raw,body['name'])}
            elif path=='/api/drive/assign':
                result=SYNC.assign_photo(body['id'],body['metadata'])
            elif path=='/api/drive/apply-source':
                result=SYNC.apply_source(body['id'],body['reviewer'])
            else:
                return self.send(404,{'error':'Not found'})
            return self.send(200,result)
        except Exception as error:
            text=str(error)[:180] if isinstance(error,ValueError) else 'Drive operation failed. Local work is retained.'
            return self.send(400,{'error':text})


class DesktopServer(editor.ReviewServer):
    # POSIX needs SO_REUSEADDR to restart after recently closed local HTTP
    # connections. It does not permit sharing an active listening socket.
    # Windows keeps its exclusive-bind behavior instead.
    allow_reuse_address=os.name!='nt'

    def server_bind(self):
        if os.name=='nt':
            self.socket.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
        super().server_bind()


def main():
    global SYNC
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port',type=int,default=8791)
    default_data, default_private = default_paths()
    parser.add_argument('--data',type=Path,default=default_data)
    parser.add_argument('--private',type=Path,default=default_private)
    parser.add_argument('--open',action='store_true')
    parser.add_argument('--fixture-service',help='Explicit isolated loopback fake service, never Google')
    parser.add_argument('--interval',type=int,default=20)
    args=parser.parse_args()
    if args.private.resolve()==editor.ROOT or editor.ROOT in args.private.resolve().parents:
        parser.error('Private configuration and credentials must stay outside the source repository')
    if args.port==8766:
        parser.error('Desktop mode uses its own port; production port 8766 is reserved')
    editor.DATA=args.data.resolve();editor.DATA.mkdir(parents=True,exist_ok=True)
    if (editor.DATA/'team.sqlite3').exists():
        parser.error('Choose an independent desktop cache; never sync a team database')
    editor.acquire_writer(editor.DATA)
    if args.private.resolve()==editor.DATA or editor.DATA in args.private.resolve().parents:
        parser.error('Private credentials must stay outside the editor data folder')
    SYNC=DriveSync(editor.DATA,args.private.resolve(),args.fixture_service,max(2,args.interval))
    editor.export_batch=checked_export
    server=DesktopServer(('127.0.0.1',args.port),DesktopHandler)
    threading.Thread(target=SYNC.run,daemon=True).start()
    url=f'http://127.0.0.1:{args.port}'
    print('Mask Review desktop: '+url,flush=True)
    if args.fixture_service:
        print('ISOLATED FIXTURE TRANSPORT. This is not actual Google verification.',flush=True)
    if args.open:
        webbrowser.open(url)
    server.serve_forever()


if __name__=='__main__':
    main()
