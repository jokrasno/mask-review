"""Local desktop OAuth and immutable Drive revision transport. No database sync."""
import base64
import ctypes
import hashlib
import io
import json
import os
import re
from pathlib import Path
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile

from PIL import Image
import server as editor
from credential_store import CredentialStore
from desktop_platform import private_directory, private_write

SCOPE = 'https://www.googleapis.com/auth/drive'
FOLDER_KEYS = ('library', 'originals', 'labels', 'incoming')


def validate_library(value):
    if (not isinstance(value, dict) or set(value) != {'format', 'folders'}
            or value.get('format') != 'mask-review-library-config-v1'
            or not isinstance(value.get('folders'), dict) or set(value['folders']) != set(FOLDER_KEYS)):
        raise ValueError('Choose a library configuration with the documented format and four folder IDs')
    folders = value['folders']
    if any(not isinstance(fid, str) or not re.fullmatch(r'[A-Za-z0-9_-]{10,160}', fid)
           or any(word in fid.lower() for word in ('replace', 'placeholder', 'your_', 'example')) for fid in folders.values()):
        raise ValueError('Library folder IDs must be real Drive IDs, without URLs or placeholders')
    if len(set(folders.values())) != len(FOLDER_KEYS):
        raise ValueError('Choose four distinct library folders')
    return {'format': 'mask-review-library-config-v1', 'folders': dict(folders)}


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def atomic(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    with temp.open('xb') as out:
        out.write(raw)
        out.flush()
        os.fsync(out.fileno())
    os.replace(temp, path)


def save(path, value):
    atomic(path, json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode())


def load(path, default):
    return json.loads(path.read_text('utf-8')) if path.exists() else default


def protect(raw, decrypt=False):
    """Current-user DPAPI, with no UI and no token fallback on unsupported OS."""
    if os.name != 'nt':
        raise ValueError('Credential persistence requires Windows DPAPI on this desktop build')
    from ctypes import wintypes
    class Blob(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]
    buf = ctypes.create_string_buffer(raw)
    source = Blob(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))
    result = Blob()
    fn = ctypes.windll.crypt32.CryptUnprotectData if decrypt else ctypes.windll.crypt32.CryptProtectData
    ok = fn(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result))
    if not ok:
        raise ValueError('Windows could not unlock the private Google credentials for this user')
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        ctypes.windll.kernel32.LocalFree(result.data)


class GoogleDrive:
    def __init__(self, private, fixture=None):
        self.private, self.fixture = private, fixture
        self.token_path = private / 'google-token.dpapi'
        self.client_path = private / 'google-client.json'
        self.oauth_pending = None
        self.credential_lock = threading.RLock()
        self.credential_store = CredentialStore(private, protect)
        self.credential_error = ''
        try:
            self.tokens = self.credential_store.load()
        except (ValueError, OSError):
            self.tokens = {}
            self.credential_error = self.credential_store._message()
        if fixture:
            parsed = urllib.parse.urlparse(fixture)
            if parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or parsed.path not in ('', '/') or parsed.username:
                raise ValueError('Fixture transport must be an explicit loopback HTTP origin')
        self.api = fixture + '/drive/v3' if fixture else 'https://www.googleapis.com/drive/v3'
        self.upload = fixture + '/upload/drive/v3' if fixture else 'https://www.googleapis.com/upload/drive/v3'
        self.auth = fixture + '/authorize' if fixture else 'https://accounts.google.com/o/oauth2/v2/auth'
        self.token_url = fixture + '/token' if fixture else 'https://oauth2.googleapis.com/token'

    def client(self):
        value = load(self.client_path, {}).get('installed', {})
        if not str(value.get('client_id', '')).endswith('.apps.googleusercontent.com'):
            raise ValueError('Add a Google Desktop OAuth client JSON in Google setup first')
        return value

    def configure(self, value):
        installed = value.get('installed') if isinstance(value, dict) else None
        if not isinstance(installed, dict) or not str(installed.get('client_id', '')).endswith('.apps.googleusercontent.com'):
            raise ValueError('Choose the downloaded OAuth JSON for a Desktop app client')
        # Public desktop client configuration is not a refresh credential.
        private_write(self.client_path, json.dumps({'installed': {k: installed[k] for k in ('client_id', 'client_secret') if k in installed}}).encode())

    def begin(self, port):
        with self.credential_lock:
            return self._begin(port)

    def _begin(self, port):
        client = self.client()
        verifier, state = secrets.token_urlsafe(64), secrets.token_urlsafe(32)
        # Match Google's installed-app loopback pattern (root path, local port).
        redirect = f'http://127.0.0.1:{port}/'
        self.oauth_pending = dict(verifier=verifier, state=state, redirect=redirect, expires=time.time()+600)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
        return self.auth + '?' + urllib.parse.urlencode(dict(client_id=client['client_id'], redirect_uri=redirect, response_type='code', scope=SCOPE, access_type='offline', prompt='consent', state=state, code_challenge=challenge, code_challenge_method='S256'))

    def exchange(self, query):
        with self.credential_lock:
            return self._exchange(query)

    def _exchange(self, query):
        pending = self.oauth_pending
        if not pending or time.time() > pending['expires'] or not secrets.compare_digest(query.get('state', [''])[0], pending['state']):
            raise ValueError('Google sign-in expired or state did not match. Start sign-in again.')
        self.oauth_pending = None
        if query.get('error') or not query.get('code'):
            raise ValueError('Google sign-in was not completed. You can continue working locally.')
        params = dict(client_id=self.client()['client_id'], code=query['code'][0], code_verifier=pending['verifier'], redirect_uri=pending['redirect'], grant_type='authorization_code')
        if self.client().get('client_secret'):
            params['client_secret'] = self.client()['client_secret']
        self.store_tokens(self.raw(self.token_url, params, form=True))
        if SCOPE not in self.tokens.get('scope', '').split():
            self.disconnect()
            raise ValueError('Google did not grant the required existing-library Drive scope')

    def store_tokens(self, tokens):
        with self.credential_lock:
            return self._store_tokens(tokens)

    def _store_tokens(self, tokens):
        if (not isinstance(tokens, dict) or not isinstance(tokens.get('access_token'), str)
                or not tokens['access_token']
                or any(k in tokens and not isinstance(tokens[k], str) for k in ('refresh_token', 'scope'))):
            raise ValueError('Google returned no usable access credential')
        tokens = dict(tokens)
        try:
            lifetime = int(tokens.get('expires_in', 3600))
            if not 0 < lifetime <= 365 * 24 * 3600:
                raise ValueError('Invalid lifetime')
        except (ValueError, TypeError, OverflowError):
            raise ValueError('Google returned an invalid credential lifetime. Reconnect to try again.') from None
        tokens['expires_at'] = time.time()+lifetime-60
        if not tokens.get('refresh_token') and self.tokens.get('refresh_token'):
            tokens['refresh_token'] = self.tokens['refresh_token']
        if 'scope' not in tokens and self.tokens.get('scope'):
            tokens['scope'] = self.tokens['scope']
        try:
            self.credential_store.save(tokens)
        except ValueError as error:
            self.tokens = {}
            self.credential_error = str(error)
            raise
        self.tokens = tokens
        self.credential_error = ''

    def disconnect(self):
        with self.credential_lock:
            self.oauth_pending = None
            self.tokens = {}
            try:
                self.credential_store.delete()
            except ValueError as error:
                self.credential_error = str(error)
                raise
            self.credential_error = ''

    def raw(self, url, body=None, method=None, form=False, headers=None, binary=False, response_headers=False):
        if not getattr(self, 'library_ready', lambda: False)():
            raise ValueError('Import a private library configuration before connecting to Drive')
        headers = dict(headers or {})
        if form:
            body = urllib.parse.urlencode(body).encode()
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
        elif isinstance(body, dict):
            body = json.dumps(body).encode()
            headers['Content-Type'] = 'application/json'
        req = urllib.request.Request(url, body, headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=25) as response:
                raw = response.read(editor.MAX_ZIP+1)
                if len(raw) > editor.MAX_ZIP:
                    raise ValueError('Drive response exceeded the bounded archive limit')
                result = raw if binary else json.loads(raw or b'{}')
                return (result,dict(response.headers)) if response_headers else result
        except urllib.error.HTTPError as error:
            # Never return OAuth bodies, headers, tokens or full URLs to the UI/log.
            raise ValueError(f'Google request failed ({error.code}). Check account access or reconnect.') from None
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
            raise ValueError('Drive is unavailable. Saved work stays queued on this computer.') from None

    def request(self, suffix, body=None, method=None, binary=False, upload=False, headers=None, response_headers=False):
        # A refresh and its durable save must finish before disconnect can delete
        # credentials. A late network response can never restore a disconnected login.
        with self.credential_lock:
            return self._request(suffix, body, method, binary, upload, headers, response_headers)

    def _request(self, suffix, body=None, method=None, binary=False, upload=False, headers=None, response_headers=False):
        if not self.tokens:
            raise ValueError('Sign in to Google to sync. Local saved work is retained.')
        if self.tokens.get('expires_at', 0) <= time.time():
            if not self.tokens.get('refresh_token'):
                raise ValueError('Google sign-in expired. Reconnect to resume queued uploads.')
            params = dict(client_id=self.client()['client_id'], refresh_token=self.tokens['refresh_token'], grant_type='refresh_token')
            if self.client().get('client_secret'):
                params['client_secret'] = self.client()['client_secret']
            self.store_tokens(self.raw(self.token_url, params, form=True))
        headers = dict(headers or {}, Authorization='Bearer '+self.tokens['access_token'])
        return self.raw((self.upload if upload else self.api)+suffix, body, method, headers=headers, binary=binary, response_headers=response_headers)

    def listing(self, folder):
        files, page = [], None
        for _ in range(100):
            query = dict(q=f"'{folder}' in parents and trashed = false", fields='nextPageToken,files(id,name,mimeType,size,md5Checksum,appProperties,parents)', pageSize=100, supportsAllDrives='true', includeItemsFromAllDrives='true')
            if page:
                query['pageToken'] = page
            response = self.request('/files?'+urllib.parse.urlencode(query))
            files.extend(response.get('files', []))
            page = response.get('nextPageToken')
            if not page:
                return files
        raise ValueError('Library contains too many entries for this bounded client')

    def download(self, fid):
        if not fid or not all(c.isalnum() or c in '-_' for c in fid):
            raise ValueError('Invalid Drive file identity')
        meta = self.request('/files/'+fid+'?fields=id,name,mimeType,size,md5Checksum,parents&supportsAllDrives=true')
        if int(meta.get('size', editor.MAX_ZIP+1)) > editor.MAX_ZIP:
            raise ValueError('Selected Drive file exceeds 100 MiB')
        raw = self.request('/files/'+fid+'?alt=media&supportsAllDrives=true', binary=True)
        if meta.get('md5Checksum') and hashlib.md5(raw).hexdigest() != meta['md5Checksum']:
            raise ValueError('Downloaded Drive file checksum did not match')
        return meta, raw

    def upload_immutable(self, item, raw):
        """Pre-generated Drive ID is durably stored before CREATE; retries verify it."""
        if not item.get('drive_id'):
            item['drive_id'] = self.request('/files/generateIds?count=1&space=drive')['ids'][0]
            return False
        try:
            meta = self.request('/files/'+item['drive_id']+'?fields=id,size,md5Checksum,appProperties&supportsAllDrives=true')
        except ValueError as error:
            if '(404)' not in str(error):
                raise
        else:
            if meta.get('md5Checksum') != hashlib.md5(raw).hexdigest() or meta.get('appProperties', {}).get('maskRevision') != item['id']:
                raise ValueError('Drive retry identity has different bytes; upload stopped for review')
            return True
        metadata = dict(id=item['drive_id'], name=item['name'], parents=[item['folder']], appProperties={'maskRevision': item['id']})
        if len(raw)>5*1024*1024:
            _, headers=self.request('/files?uploadType=resumable&supportsAllDrives=true&fields=id,md5Checksum',metadata,method='POST',upload=True,response_headers=True,headers={'X-Upload-Content-Type':item['mime'],'X-Upload-Content-Length':str(len(raw))})
            location=next((value for key,value in headers.items() if key.lower()=='location'),None)
            parsed=urllib.parse.urlparse(location or '')
            allowed=urllib.parse.urlparse(self.upload)
            if parsed.scheme!=allowed.scheme or parsed.netloc!=allowed.netloc or not parsed.path.startswith('/upload/'):
                raise ValueError('Drive returned an unexpected resumable upload address')
            # Session URL stays in memory only. A lost session restarts with the
            # same durable Drive file ID and verifies completed bytes first.
            created=self.raw(location,raw,method='PUT',headers={'Content-Type':item['mime'],'Content-Length':str(len(raw)),'Authorization':'Bearer '+self.tokens['access_token']})
            if created.get('id')!=item['drive_id'] or created.get('md5Checksum')!=hashlib.md5(raw).hexdigest():
                raise ValueError('Drive resumable upload checksum did not match; retained for retry')
            return True
        boundary = 'mask_'+uuid.uuid4().hex
        body = (f'--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n'.encode()+json.dumps(metadata).encode()+f'\r\n--{boundary}\r\nContent-Type: {item["mime"]}\r\n\r\n'.encode()+raw+f'\r\n--{boundary}--\r\n'.encode())
        created=self.request('/files?uploadType=multipart&supportsAllDrives=true&fields=id,md5Checksum', body, method='POST', upload=True, headers={'Content-Type': 'multipart/related; boundary='+boundary})
        if created.get('id')!=item['drive_id'] or created.get('md5Checksum')!=hashlib.md5(raw).hexdigest():
            raise ValueError('Drive upload checksum did not match; retained for retry')
        return True


class DriveSync:
    def __init__(self, data, private, fixture=None, interval=20):
        self.data, self.root, self.private = data, data/'drive-sync', private
        self.root.mkdir(parents=True, exist_ok=True)
        private_directory(self.private)
        self.path = self.root/'journal.json'
        self.j = load(self.path, dict(format='mask-review-drive-journal-v1', device=uuid.uuid4().hex, frames={}, revisions={}, outbox=[], imports={}, quarantine={}, account=None))
        self.j.setdefault('sources',{})
        self.config_path = private / 'library-config.json'
        self.library_config = validate_library(load(self.config_path, None)) if self.config_path.exists() else None
        if self.library_config:
            for item in self.j['outbox']:
                if item.get('folder') is None:
                    item['folder'] = self.folders()['incoming' if item['path'].startswith('incoming/') else 'labels']
        self.drive = GoogleDrive(private, fixture)
        self.drive.library_ready = lambda: self.library_config is not None
        self.interval, self.error, self.activity, self.last_sync = interval, '', '', None
        self.guard = threading.RLock()
        self.wake = threading.Event()
        self.paused = self.j.get('paused',False)
        self.scan_local()

    def persist(self):
        save(self.path, self.j)

    def frame_key(self, sample):
        return digest(json.dumps({k: sample[k] for k in ('id','image_sha256','suggestion_sha256','dataset','session','run','model','split')}, sort_keys=True).encode())

    def signature(self, sample, raw):
        return digest(json.dumps(dict(mask=digest(raw), state=sample['state'], reviewer=sample['reviewer'], note=sample['note'], revision=sample['revision'], history=sample['history']), sort_keys=True).encode())

    def scan_local(self):
        with editor.LOCK, self.guard:
            for batch in editor.state()['batches']:
                bid = batch['id']
                for index, sample in enumerate(editor.read_batch(bid)['samples']):
                    source_key = self.frame_key(sample)
                    key=next((key for key,entry in self.j['frames'].items() if entry['batch']==bid and entry['index']==index),None)
                    if key is None:
                        key=source_key if source_key not in self.j['frames'] else digest(f'{source_key}:{bid}:{index}'.encode())
                    raw = editor.working_bytes(bid, index)
                    signature = self.signature(sample, raw)
                    frame = self.j['frames'].setdefault(key, dict(source_key=source_key,batch=bid, index=index, current=None, signature=signature))
                    if frame['signature'] != signature:
                        self.make_revision(key, sample, raw, [frame['current']] if frame['current'] else [])
            self.persist()

    def make_revision(self, key, sample, raw, parents):
        editor.decode_label(raw, (sample['width'], sample['height']))
        rid = uuid.uuid4().hex
        manifest = dict(format='mask-review-revision-v1', taxonomy=editor.TAXONOMY, id=rid, frame=self.j['frames'][key].get('source_key',key), parents=sorted(set(parents)), device=self.j['device'], author=self.j.get('account'), at=editor.stamp(), image_sha256=sample['image_sha256'], suggestion_sha256=sample['suggestion_sha256'], width=sample['width'], height=sample['height'], mask_sha256=digest(raw), state=sample['state'], reviewer=sample['reviewer'], note=sample['note'], source={k:sample[k] for k in ('id','dataset','session','run','split','model')}, audit=sample['history'])
        if manifest['state'] == 'approved':
            last = sample['history'][-1] if sample['history'] else {}
            if last.get('action') != 'approved' or last.get('mask_sha256') != digest(raw) or not sample['reviewer'].strip():
                raise ValueError('Cannot sync an approval without a matching native mask audit')
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('revision.json', json.dumps(manifest, sort_keys=True))
            archive.writestr('mask.png', raw)
        atomic(self.root/'packages'/f'{rid}.zip', buffer.getvalue())
        self.j['revisions'][rid] = manifest
        self.j['outbox'].append(dict(id=rid, name=f'mask-revision-{rid}.zip', folder=self.folders()['labels'], mime='application/zip', path=f'packages/{rid}.zip', sha256=digest(buffer.getvalue())))
        frame = self.j['frames'][key]
        frame.update(current=rid, signature=self.signature(sample, raw))
        self.persist()
        self.wake.set()
        return rid

    def folders(self):
        return dict(self.library_config['folders']) if self.library_config else dict.fromkeys(FOLDER_KEYS)

    def require_library(self):
        if self.library_config is None:
            raise ValueError('Import the private library configuration in Google setup first')

    def configure_library(self, value):
        value = validate_library(value)
        with editor.LOCK, self.guard:
            if self.library_config and value != self.library_config:
                raise ValueError('This workspace is bound to another library. Use a new data and private directory to switch libraries')
            private_write(self.config_path, json.dumps(value, sort_keys=True, indent=2).encode())
            self.library_config = value
            for item in self.j['outbox']:
                if item.get('folder') is None:
                    item['folder'] = self.folders()['incoming' if item['path'].startswith('incoming/') else 'labels']
            self.persist()
            return {'library_configured': True}

    def heads(self, key):
        key=self.j['frames'].get(key,{}).get('source_key',key)
        revs = {rid: r for rid, r in self.j['revisions'].items() if r['frame'] == key}
        parents = {p for r in revs.values() for p in r['parents']}
        return sorted(set(revs)-parents)

    def updates(self):
        result = []
        for key, frame in self.j['frames'].items():
            heads = self.heads(key)
            if heads and heads != [frame['current']]:
                sample = editor.read_batch(frame['batch'])['samples'][frame['index']]
                result.append(dict(frame=key, filename=sample['filename'], batch=frame['batch'], index=frame['index'], heads=heads, conflict=len(heads)>1, local=frame['current'], choices=[dict(id=rid, reviewer=self.j['revisions'][rid]['reviewer'], state=self.j['revisions'][rid]['state'], at=self.j['revisions'][rid]['at'], device=self.j['revisions'][rid]['device'][:8], mask_url='/api/drive/preview/'+rid) for rid in sorted(set(heads+[frame['current']])-{None})]))
        return result

    def status(self):
        with editor.LOCK, self.guard:
            try:
                self.drive.client()
                configured = True
            except ValueError:
                configured = False
            active=editor.state()
            versions=None
            for key,frame in self.j['frames'].items():
                if frame['batch']==active['active_batch'] and frame['index']==active['active_sample']:
                    versions=dict(frame=key,heads=self.heads(key),batch=frame['batch'],index=frame['index'],current=frame['current'],items=[dict(id=rid,reviewer=rev['reviewer'],state=rev['state'],at=rev['at']) for rid,rev in self.j['revisions'].items() if rev['frame']==frame.get('source_key',key)])
                    break
            incoming=[]
            for key,item in self.j['quarantine'].items():
                assignments=[dict(id=sid,metadata=s['metadata'],reviewer=s.get('reviewer',''),at=s['at']) for sid,s in self.j['sources'].items() if s['image_sha256']==key]
                known=sorted(s['id'] for s in assignments)
                conflict=len(assignments)>1 and item.get('resolved_assignments')!=known
                if not item.get('assigned') or conflict:
                    incoming.append(dict(id=key,name=item['name'],sha256=key,width=item['width'],height=item['height'],assignments=assignments,assignment_conflict=conflict,assigned=bool(item.get('assigned'))))
            return dict(enabled=True, library_configured=self.library_config is not None, configured=configured, authenticated=bool(self.drive.tokens), fixture=bool(self.drive.fixture), account=self.j.get('account') if self.drive.tokens else None, queued=len(self.j['outbox']), paused=self.paused, error=self.drive.credential_error or self.error, activity=self.activity, last_sync=self.last_sync, folders=self.folders() if self.library_config else {}, imported=len(self.j['imports']), updates=self.updates(), versions=versions, incoming=incoming, scope=SCOPE)

    def library_files(self):
        self.require_library()
        return [dict(id=f['id'], name=f['name'], size=f.get('size'), mime=f['mimeType']) for f in self.drive.listing(self.folders()['labels']) if f['mimeType']=='application/zip' and not f['name'].startswith(('mask-revision-','mask-source-'))]

    def import_library(self, fid):
        self.require_library()
        meta, raw = self.drive.download(fid)
        if meta.get('mimeType') != 'application/zip' or self.folders()['labels'] not in meta.get('parents', []):
            raise ValueError('Select a ZIP from this library label folder')
        sha = digest(raw)
        with editor.LOCK, self.guard:
            if self.j['imports'].get(sha,{}).get('complete'):
                return dict(already_imported=True)
            archive = zipfile.ZipFile(io.BytesIO(raw))
            total = sum(i.file_size for i in archive.infolist())
            if total > editor.MAX_UNPACKED or len(archive.infolist())>500:
                raise ValueError('Library snapshot exceeds bounded import limits')
            for name in archive.namelist():
                editor.safe_member(name.rstrip('/'))
            manifest = json.loads(archive.read('library-manifest.json')) if 'library-manifest.json' in archive.namelist() else None
            if manifest:
                if manifest.get('format') != 'mask-review-library-snapshot-v1' or manifest.get('mask_taxonomy') != editor.TAXONOMY:
                    raise ValueError('Unsupported library snapshot contract')
                packages = [archive.read(name) for name in archive.namelist() if name.startswith('review-packages/') and name.endswith('.zip')]
            else:
                packages = [raw]
            if not packages or len(packages)>20:
                raise ValueError('Snapshot contains no bounded review packages')
            # Every nested package is validated in an isolated temporary editor root
            # before committing any to the actual local queue.
            original_data = editor.DATA
            import tempfile
            try:
                with tempfile.TemporaryDirectory(prefix='mask-validate-') as temp:
                    editor.DATA = Path(temp)
                    existing=load(original_data/'state.json',dict(batches=[],active_batch=None,active_sample=0,reviewer=''))
                    save(editor.DATA/'state.json',existing)
                    for batch in existing['batches']:
                        save(editor.DATA/'batches'/batch['id']/'batch.json',load(original_data/'batches'/batch['id']/'batch.json',{}))
                    for package in packages:
                        editor.import_zip(package)
            finally:
                editor.DATA = original_data
            receipt=self.j['imports'].setdefault(sha,dict(drive_id=fid,batches=[],packages={},at=editor.stamp(),complete=False))
            for package in packages:
                package_sha=digest(package)
                if package_sha in receipt['packages']:
                    continue
                result = editor.import_zip(package)
                receipt['batches'].append(result['active_batch']);receipt['packages'][package_sha]=result['active_batch'];self.persist()
            atomic(self.root/'cache'/f'{sha}.zip', raw)
            receipt['complete']=True
            self.scan_local()
            self.persist()
            return dict(imported_batches=receipt['batches'])

    def receive_revision(self, fid, raw):
        if len(raw)>18*1024*1024:
            raise ValueError('Remote revision exceeds 18 MiB')
        archive = zipfile.ZipFile(io.BytesIO(raw))
        if set(archive.namelist()) != {'revision.json','mask.png'} or sum(i.file_size for i in archive.infolist())>20*1024*1024:
            raise ValueError('Remote revision has unexpected assets')
        if archive.getinfo('revision.json').file_size>2*1024*1024:
            raise ValueError('Remote revision manifest exceeds limit')
        rev, mask = json.loads(archive.read('revision.json')), archive.read('mask.png')
        if not isinstance(rev,dict):
            raise ValueError('Remote revision manifest must be an object')
        rid, key = rev.get('id',''), rev.get('frame','')
        if rev.get('format') != 'mask-review-revision-v1' or rev.get('taxonomy') != editor.TAXONOMY or len(rid)!=32 or any(c not in '0123456789abcdef' for c in rid):
            raise ValueError('Unsupported remote revision identity')
        if not isinstance(rev.get('parents'),list) or len(rev['parents'])>100 or rid in rev['parents'] or any(not isinstance(p,str) or len(p)!=32 or any(c not in '0123456789abcdef' for c in p) for p in rev['parents']):
            raise ValueError('Invalid revision parent graph')
        if rev.get('state') not in ('pending','edited','approved','rejected','skipped') or not isinstance(rev.get('audit'),list):
            raise ValueError('Invalid remote review state')
        if self.frame_key(dict(rev['source'],image_sha256=rev['image_sha256'],suggestion_sha256=rev['suggestion_sha256'])) != key:
            raise ValueError('Revision source identity does not match')
        editor.decode_label(mask, (rev['width'],rev['height']))
        if digest(mask)!=rev['mask_sha256']:
            raise ValueError('Remote mask checksum failed')
        if rev['state']=='approved':
            last = rev['audit'][-1] if rev['audit'] else {}
            if not rev.get('reviewer','').strip() or last.get('action')!='approved' or last.get('mask_sha256')!=digest(mask):
                raise ValueError('Remote approval audit does not match mask')
        with editor.LOCK, self.guard:
            if rid in self.j['revisions']:
                if self.j['revisions'][rid] != rev:
                    raise ValueError('Remote revision identity was reused with different metadata')
                return
            def contains(start,target,seen):
                if start==target:
                    return True
                if start in seen:
                    return False
                seen.add(start)
                return any(contains(p,target,seen) for p in self.j['revisions'].get(start,{}).get('parents',[]))
            if any(contains(parent,rid,set()) for parent in rev['parents']):
                raise ValueError('Remote revision graph contains a cycle')
            frame = self.j['frames'].get(key)
            if frame:
                sample = editor.read_batch(frame['batch'])['samples'][frame['index']]
                if (sample['width'],sample['height'])!=(rev['width'],rev['height']):
                    raise ValueError('Remote revision dimensions do not match cached original')
            atomic(self.root/'packages'/f'{rid}.zip', raw)
            self.j['revisions'][rid] = rev
            self.persist()

    def reconcile(self, key, selected, expected, reviewer):
        with editor.LOCK, self.guard:
            frame = self.j['frames'][key]
            heads = self.heads(key)
            if sorted(expected)!=heads or self.j['revisions'].get(selected,{}).get('frame')!=frame.get('source_key',key) or not reviewer.strip():
                raise ValueError('Heads changed or reviewer missing. Reopen incoming changes and review again.')
            rev = self.j['revisions'][selected]
            mask = zipfile.ZipFile(self.root/'packages'/f'{selected}.zip').read('mask.png')
            batch = editor.read_batch(frame['batch'])
            sample = batch['samples'][frame['index']]
            sample.update(state='edited', reviewer='', revision=sample['revision']+1, note=rev.get('note','')[:500])
            sample['provenance']['drive_reconcile'] = dict(selected=selected, parents=heads, reviewer=reviewer, at=editor.stamp(), remote_audit=rev['audit'])
            editor.append_event(batch,sample,dict(action='drive_reconcile',at=editor.stamp(),revision=sample['revision'],reviewer=reviewer,parents=heads,selected=selected))
            # Invalidate approval before changing bytes, matching existing editor safety.
            editor.write_json(editor.batch_path(frame['batch'])/'batch.json', batch)
            atomic(editor.batch_path(frame['batch'])/str(frame['index'])/'working.png',mask)
            self.make_revision(key,sample,mask,heads)
            return dict(batch=frame['batch'], index=frame['index'])

    def quarantine_photo(self, raw, name, drive_id=None):
        image = Image.open(io.BytesIO(raw)); image.load()
        if image.format not in ('PNG','JPEG') or getattr(image,'n_frames',1)!=1 or image.width*image.height>editor.MAX_PIXELS:
            raise ValueError('Incoming photos must be bounded static PNG/JPEG images')
        sha=digest(raw)
        with self.guard:
            key=sha
            if key not in self.j['quarantine']:
                atomic(self.root/'incoming'/f'{sha}.bin',raw)
                self.j['quarantine'][key]=dict(name=Path(name).name[:200],sha256=sha,width=image.width,height=image.height,drive_id=drive_id,assigned=False,at=editor.stamp())
                if not drive_id:
                    self.j['outbox'].append(dict(id=sha,name='incoming-'+sha+('.png' if image.format=='PNG' else '.jpg'),folder=self.folders()['incoming'],mime='image/png' if image.format=='PNG' else 'image/jpeg',path=f'incoming/{sha}.bin',sha256=sha))
                self.persist();self.wake.set()
            elif drive_id and not self.j['quarantine'][key].get('drive_id'):
                self.j['quarantine'][key]['drive_id']=drive_id;self.persist()
        return key

    def assign_photo(self, key, metadata):
        with editor.LOCK, self.guard:
            item=self.j['quarantine'][key]
            if item.get('assigned'):
                return dict(batch=item['assigned'])
            reviewer=editor.state().get('reviewer') or (self.j.get('account') or {}).get('displayName','')
            if not reviewer.strip():
                raise ValueError('Enter your reviewer name before assigning capture metadata')
            for field in ('dataset','session','run','split','provenance'):
                if not isinstance(metadata.get(field),str) or not metadata[field].strip() or len(metadata[field])>200:
                    raise ValueError('Assign dataset, session, run, split and rights/provenance before review')
            if metadata['split'] not in ('development','validation'):
                raise ValueError('Incoming editor photos may be assigned development or validation only')
            suggestion=io.BytesIO();Image.new('L',(item['width'],item['height']),0).save(suggestion,format='PNG')
            manifest=dict(format='mask-review-v1',taxonomy=editor.TAXONOMY,classes=editor.CLASSES,title='Incoming photos',samples=[dict(id=key,image='image.bin',filename=item['name'],suggestion='suggestion.png',model='unlabeled incoming photo',dataset=metadata['dataset'],session=metadata['session'],run=metadata['run'],split=metadata['split'],incoming_provenance=metadata['provenance'],incoming_drive_id=item.get('drive_id'))])
            content=io.BytesIO()
            with zipfile.ZipFile(content,'w',zipfile.ZIP_DEFLATED) as z:
                z.writestr('manifest.json',json.dumps(manifest));z.writestr('image.bin',(self.root/'incoming'/f'{key}.bin').read_bytes());z.writestr('suggestion.png',suggestion.getvalue())
            sid=digest(json.dumps(dict(image_sha256=key,metadata=metadata),sort_keys=True).encode())[:32]
            source=dict(format='mask-review-source-v1',id=sid,image_sha256=key,width=item['width'],height=item['height'],metadata=metadata,reviewer=reviewer,author=self.j.get('account'),at=editor.stamp(),review_sha256=digest(content.getvalue()))
            source_buffer=io.BytesIO()
            with zipfile.ZipFile(source_buffer,'w',zipfile.ZIP_DEFLATED) as archive:
                archive.writestr('source.json',json.dumps(source,sort_keys=True));archive.writestr('review.zip',content.getvalue())
            source_raw=source_buffer.getvalue();atomic(self.root/'sources'/f'{sid}.zip',source_raw)
            self.j['sources'][sid]=source
            self.j['outbox'].append(dict(id=sid,name=f'mask-source-{sid}.zip',folder=self.folders()['labels'],mime='application/zip',path=f'sources/{sid}.zip',sha256=digest(source_raw)))
            self.persist();self.wake.set()
            return self.apply_source(sid,reviewer)

    def receive_source(self, raw):
        archive=zipfile.ZipFile(io.BytesIO(raw))
        if set(archive.namelist())!={'source.json','review.zip'} or sum(x.file_size for x in archive.infolist())>editor.MAX_UNPACKED or archive.getinfo('source.json').file_size>65536:
            raise ValueError('Invalid shared photo source package')
        source=json.loads(archive.read('source.json'));review=archive.read('review.zip')
        if not isinstance(source,dict):
            raise ValueError('Shared photo source manifest must be an object')
        metadata=source.get('metadata',{})
        sid=digest(json.dumps(dict(image_sha256=source.get('image_sha256'),metadata=metadata),sort_keys=True).encode())[:32]
        if source.get('format')!='mask-review-source-v1' or source.get('id')!=sid or source.get('review_sha256')!=digest(review):
            raise ValueError('Shared photo assignment identity/checksum failed')
        for field in ('dataset','session','run','split','provenance'):
            if not isinstance(metadata.get(field),str) or not metadata[field].strip() or len(metadata[field])>200:
                raise ValueError('Shared photo assignment needs bounded capture metadata')
        if metadata['split'] not in ('validation','development'):
            raise ValueError('Shared photo assignment cannot admit TRAIN data')
        package=zipfile.ZipFile(io.BytesIO(review))
        if set(package.namelist())!={'manifest.json','image.bin','suggestion.png'} or sum(x.file_size for x in package.infolist())>editor.MAX_UNPACKED or package.getinfo('manifest.json').file_size>65536:
            raise ValueError('Shared photo assignment has unexpected assets')
        manifest=json.loads(package.read('manifest.json'));samples=manifest.get('samples',[])
        if manifest.get('format')!='mask-review-v1' or manifest.get('taxonomy')!=editor.TAXONOMY or manifest.get('classes')!=editor.CLASSES or len(samples)!=1:
            raise ValueError('Shared photo assignment taxonomy/sample mismatch')
        sample=samples[0]
        if any(sample.get(k)!=metadata[k] for k in ('dataset','session','run','split')) or sample.get('incoming_provenance')!=metadata['provenance'] or sample.get('id')!=source['image_sha256'] or sample.get('image')!='image.bin' or sample.get('suggestion')!='suggestion.png' or sample.get('model')!='unlabeled incoming photo' or sample.get('mask'):
            raise ValueError('Shared photo assignment changed capture metadata or inherited a mask')
        photo=package.read('image.bin');image=Image.open(io.BytesIO(photo));image.load()
        if image.format not in ('PNG','JPEG') or image.size!=(source['width'],source['height']) or digest(photo)!=source['image_sha256']:
            raise ValueError('Shared photo native image checksum/dimensions failed')
        suggestion=editor.decode_label(package.read('suggestion.png'),image.size)
        if any(suggestion.tobytes()):
            raise ValueError('Incoming source suggestions must start unresolved')
        with editor.LOCK,self.guard:
            if sid in self.j['sources']:
                # Independent people may publish the same assignment identity.
                # Preserve the first verified package; identical capture metadata
                # and photo bytes give the same editor frame identity.
                return
            self.quarantine_photo(photo,sample.get('filename','Incoming photo'))
            atomic(self.root/'sources'/f'{sid}.zip',raw);self.j['sources'][sid]=source;self.persist()

    def apply_source(self,sid,reviewer):
        with editor.LOCK,self.guard:
            source=self.j['sources'][sid];item=self.j['quarantine'][source['image_sha256']]
            if not reviewer.strip():
                raise ValueError('A reviewer name is required to choose capture metadata')
            known=sorted(key for key,s in self.j['sources'].items() if s['image_sha256']==source['image_sha256'])
            prior=item.get('assignment_batches',{}).get(sid)
            if item.get('assignment_id')==sid and item.get('assigned'):
                prior=item['assigned']
            already_imported=bool(prior and prior in {b['id'] for b in editor.state()['batches']})
            if not already_imported:
                # Validate/import before changing approvals or assignment selection.
                # The original session/run split guards remain authoritative.
                archive=zipfile.ZipFile(self.root/'sources'/f'{sid}.zip')
                prior=editor.import_zip(archive.read('review.zip'))['active_batch']
            if item.get('assignment_id')!=sid or item.get('resolved_assignments')!=known:
                # Capture identity changes invalidate every approval for this photo,
                # including old copies. Original pixels and split metadata stay put.
                for brief in editor.state()['batches']:
                    batch=editor.read_batch(brief['id']);changed=False
                    for sample in batch['samples']:
                        if sample['image_sha256']==source['image_sha256'] and sample['state']=='approved':
                            sample.update(state='edited',reviewer='',revision=sample['revision']+1)
                            editor.append_event(batch,sample,dict(at=editor.stamp(),action='source_assignment_changed',revision=sample['revision'],reviewer=reviewer,source_assignment=sid))
                            changed=True
                    if changed:editor.write_json(editor.batch_path(brief['id'])/'batch.json',batch)
            item.setdefault('assignment_batches',{})[sid]=prior
            item.update(assignment_id=sid,assigned=prior,resolved_assignments=known,assignment_review=dict(reviewer=reviewer,at=editor.stamp()))
            self.scan_local();self.persist();return dict(batch=prior,already_imported=already_imported)

    def unresolved_source(self,photo):
        item=self.j['quarantine'].get(photo,{})
        known=sorted(key for key,s in self.j['sources'].items() if s['image_sha256']==photo)
        return len(known)>1 and item.get('resolved_assignments')!=known

    def selected_source(self,sample):
        item=self.j['quarantine'].get(sample['image_sha256'])
        if not item or not item.get('assignment_id'):
            return True
        source=self.j['sources'][item['assignment_id']];metadata=source['metadata']
        return (all(sample.get(k)==metadata[k] for k in ('dataset','session','run','split'))
                and sample.get('provenance',{}).get('incoming_provenance')==metadata['provenance'])

    def cycle(self):
        if self.paused or not self.drive.tokens or self.library_config is None:
            return
        self.activity='Checking Drive'
        try:
            account=self.drive.request('/about?fields=user(displayName,emailAddress,permissionId)')['user']
            with self.guard:
                self.j['account']={k:account[k] for k in ('displayName','emailAddress','permissionId') if k in account}
                self.persist()
            self.scan_local()
            # Upload a bounded snapshot; new local saves remain queued for next cycle.
            with self.guard:
                pending=list(self.j['outbox'])
            for item in pending:
                if self.paused:
                    break
                if item.get('folder') not in self.folders().values():
                    raise ValueError('Queued destination does not match the configured library; retained for inspection')
                self.activity='Uploading saved review'
                raw=(self.root/item['path']).read_bytes()
                if digest(raw)!=item['sha256']:
                    raise ValueError('Queued immutable package checksum failed; retained for inspection')
                while True:
                    done=self.drive.upload_immutable(item,raw)
                    with self.guard:
                        self.persist()  # Persist generated remote ID before upload request.
                    if done:
                        break
                with self.guard:
                    self.j['outbox']=[x for x in self.j['outbox'] if x['id']!=item['id']]
                    self.persist()
            self.activity='Checking incoming reviews'
            for file in self.drive.listing(self.folders()['labels']):
                rid=file.get('appProperties',{}).get('maskRevision')
                if file['name'].startswith('mask-source-') and rid not in self.j['sources']:
                    _,raw=self.drive.download(file['id']);self.receive_source(raw)
                elif file['name'].startswith('mask-revision-') and rid not in self.j['revisions']:
                    _,raw=self.drive.download(file['id']);self.receive_revision(file['id'],raw)
            for file in self.drive.listing(self.folders()['incoming']):
                if file['mimeType'] in ('image/png','image/jpeg') and not any(x.get('drive_id')==file['id'] for x in self.j['quarantine'].values()):
                    _,raw=self.drive.download(file['id']);self.quarantine_photo(raw,file['name'],file['id'])
            self.error='';self.last_sync=editor.stamp()
        except (ValueError,KeyError,TypeError,zipfile.BadZipFile,OSError) as error:
            self.error=str(error)[:180]
        finally:
            self.activity=''

    def run(self):
        while True:
            self.wake.wait(self.interval);self.wake.clear()
            try:
                self.cycle()
            except Exception:
                self.activity='';self.error='Sync could not validate a remote item. Local saved work is retained; retry or inspect the library.'
