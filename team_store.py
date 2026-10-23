"""Private, durable team identities, sessions, claims and native mask history."""
import hashlib
import json
import secrets
import sqlite3
import time
import uuid
from pathlib import Path


class TeamError(ValueError):
    def __init__(self, message, status=409):
        super().__init__(message)
        self.status = status


class TeamStore:
    ttl = 180
    session_ttl = 7 * 86400

    def __init__(self, data):
        self.path = Path(data) / "team.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
        PRAGMA journal_mode=WAL;
        PRAGMA synchronous=FULL;
        CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY,name TEXT,role TEXT,code_hash TEXT UNIQUE,revoked INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY,user_id TEXT,csrf TEXT,expires REAL,batch TEXT,idx INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS claims(batch TEXT,idx INTEGER,session_id TEXT,expires REAL,PRIMARY KEY(batch,idx));
        CREATE TABLE IF NOT EXISTS batches(id TEXT PRIMARY KEY,body TEXT);
        CREATE TABLE IF NOT EXISTS masks(batch TEXT,idx INTEGER,body BLOB,PRIMARY KEY(batch,idx));
        CREATE TABLE IF NOT EXISTS revisions(id TEXT PRIMARY KEY,batch TEXT,idx INTEGER,revision INTEGER,at TEXT,action TEXT,user_id TEXT,reviewer TEXT,sha TEXT,body BLOB);
        ''')
        columns = {row['name'] for row in self.db.execute('PRAGMA table_info(revisions)')}
        with self.db:
            for name in ('source', 'captured_by_user_id', 'captured_by_name'):
                if name not in columns:
                    self.db.execute(f'ALTER TABLE revisions ADD COLUMN {name} TEXT')
            # Older team snapshots recorded the capturing actor as the source.
            # Preserve that capture information while identifying original pixels honestly.
            self.db.execute("UPDATE revisions SET captured_by_user_id=user_id,captured_by_name=reviewer,source='pre-team-working-mask',user_id='system:pre-team',reviewer='Pre-team snapshot' WHERE action='initial' AND source IS NULL")

    @staticmethod
    def digest(value):
        return hashlib.sha256(value.encode()).hexdigest()

    def members(self):
        return [dict(r) for r in self.db.execute('SELECT id,name,role,revoked FROM users ORDER BY name')]

    def invite(self, name, role):
        if not isinstance(name, str) or not name.strip() or len(name) > 100 or role not in ('owner', 'reviewer'):
            raise TeamError('Enter a name and a valid role', 400)
        code = secrets.token_urlsafe(32)
        user = dict(id=uuid.uuid4().hex, name=name.strip(), role=role)
        with self.db:
            self.db.execute('INSERT INTO users(id,name,role,code_hash) VALUES(?,?,?,?)', (*user.values(), self.digest(code)))
        return dict(user=user, code=code)

    def login(self, code):
        if not isinstance(code, str) or len(code) > 200:
            raise TeamError('Invalid access code', 401)
        user = self.db.execute('SELECT * FROM users WHERE code_hash=? AND revoked=0', (self.digest(code),)).fetchone()
        if not user:
            raise TeamError('Invalid access code', 401)
        sid = secrets.token_urlsafe(32)
        with self.db:
            self.db.execute('INSERT INTO sessions(id,user_id,csrf,expires) VALUES(?,?,?,?)',
                            (self.digest(sid), user['id'], secrets.token_urlsafe(32), time.time() + self.session_ttl))
        return sid

    def authenticate(self, sid):
        if not sid:
            return None
        row = self.db.execute('SELECT s.*,u.name,u.role FROM sessions s JOIN users u ON s.user_id=u.id WHERE s.id=? AND s.expires>? AND u.revoked=0',
                              (self.digest(sid), time.time())).fetchone()
        return dict(row) if row else None

    def logout(self, session):
        with self.db:
            self.db.execute('DELETE FROM claims WHERE session_id=?', (session['id'],))
            self.db.execute('DELETE FROM sessions WHERE id=?', (session['id'],))

    def revoke(self, uid, current):
        if uid == current['user_id']:
            raise TeamError('You cannot revoke your own access', 400)
        with self.db:
            self.db.execute('UPDATE users SET revoked=1 WHERE id=?', (uid,))
            self.db.execute('DELETE FROM claims WHERE session_id IN (SELECT id FROM sessions WHERE user_id=?)', (uid,))
            self.db.execute('DELETE FROM sessions WHERE user_id=?', (uid,))

    def team(self, session):
        rows = self.db.execute('SELECT c.*,u.name,u.id user_id FROM claims c JOIN sessions s ON c.session_id=s.id JOIN users u ON s.user_id=u.id WHERE c.expires>? AND s.expires>? AND u.revoked=0', (time.time(), time.time()))
        claims = [dict(batch=r['batch'], index=r['idx'], user_id=r['user_id'], name=r['name'], expires_at=r['expires'], session_owned=r['session_id'] == session['id']) for r in rows]
        result = dict(enabled=True, user=dict(id=session['user_id'], name=session['name'], role=session['role']), claims=claims, claim_ttl_seconds=self.ttl)
        if session['role'] == 'owner':
            result['members'] = self.members()
        return result

    def cursor(self, session, batch, index):
        with self.db:
            self.db.execute('UPDATE sessions SET batch=?,idx=? WHERE id=?', (batch, index, session['id']))
        session.update(batch=batch, idx=index)

    def claim(self, session, bid, index, release=False):
        now = time.time()
        with self.db:
            self.db.execute('DELETE FROM claims WHERE expires<=? OR session_id IN (SELECT id FROM sessions WHERE expires<=?)', (now, now))
            row = self.db.execute('SELECT * FROM claims WHERE batch=? AND idx=?', (bid,index)).fetchone()
            if row and row['session_id'] != session['id']:
                raise TeamError('This frame is claimed by another browser')
            if release:
                self.db.execute('DELETE FROM claims WHERE batch=? AND idx=? AND session_id=?', (bid,index,session['id']))
            else:
                self.db.execute('INSERT OR REPLACE INTO claims VALUES(?,?,?,?)', (bid,index,session['id'],now+self.ttl))

    def require_claim(self, session, bid, index):
        row = self.db.execute('SELECT * FROM claims WHERE batch=? AND idx=?', (bid,index)).fetchone()
        if not row or row['session_id'] != session['id'] or row['expires'] <= time.time():
            raise TeamError('Claim this frame before changing it')
        self.claim(session,bid,index)

    def batch(self, bid, load):
        row = self.db.execute('SELECT body FROM batches WHERE id=?', (bid,)).fetchone()
        return json.loads(row['body']) if row else load()

    def mask(self, bid, index, load):
        row = self.db.execute('SELECT body FROM masks WHERE batch=? AND idx=?', (bid,index)).fetchone()
        return bytes(row['body']) if row else load()

    def save(self, bid, index, batch, before, after, session, action, at):
        sample = batch['samples'][index]
        with self.db:
            if not self.db.execute('SELECT 1 FROM revisions WHERE batch=? AND idx=?', (bid,index)).fetchone():
                self._revision(bid,index,sample['revision']-1,before,session,'initial',at)
            self._revision(bid,index,sample['revision'],after,session,action,at)
            self.db.execute('INSERT OR REPLACE INTO masks VALUES(?,?,?)', (bid,index,after))
            self.db.execute('INSERT OR REPLACE INTO batches VALUES(?,?)', (bid,json.dumps(batch)))

    def _revision(self,bid,index,revision,body,session,action,at):
        initial = action == 'initial'
        self.db.execute('INSERT INTO revisions(id,batch,idx,revision,at,action,user_id,reviewer,sha,body,source,captured_by_user_id,captured_by_name) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                        (uuid.uuid4().hex,bid,index,revision,at,action,
                         'system:pre-team' if initial else session['user_id'],
                         'Pre-team snapshot' if initial else session['name'],
                         hashlib.sha256(body).hexdigest(),body,
                         'pre-team-working-mask' if initial else 'team-action',session['user_id'],session['name']))

    def revisions(self,bid,index):
        return [dict(r) for r in self.db.execute('SELECT id snapshot_id,revision,at,action,user_id,reviewer,sha mask_sha256,source,captured_by_user_id,captured_by_name FROM revisions WHERE batch=? AND idx=? ORDER BY rowid DESC',(bid,index))]

    def restore(self,bid,index,snapshot):
        row = self.db.execute('SELECT body FROM revisions WHERE id=? AND batch=? AND idx=?',(snapshot,bid,index)).fetchone()
        if not row:
            raise TeamError('Unknown saved revision',400)
        return bytes(row['body'])
