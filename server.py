"""Bounded local segmentation review with named human approval."""
import argparse
import base64
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import threading
import time
import uuid
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from PIL import Image
from desktop_platform import default_paths
from http.cookies import SimpleCookie
from team_store import TeamStore, TeamError
from shared_library import read_library

ROOT = Path(__file__).resolve().parent
DEFAULT_DATA = default_paths()[0]
CLASSES = {"0": "background", "1": "barrier", "2": "bollard", "3": "person", "4": "industrial_vehicle"}
TAXONOMY = "v6-object-5"
MAX_ZIP = 100 * 1024 * 1024
MAX_UNPACKED = 250 * 1024 * 1024
MAX_PIXELS = 12_000_000
Image.MAX_IMAGE_PIXELS = MAX_PIXELS
LOCK = threading.RLock()
TOKEN = uuid.uuid4().hex
DATA = DEFAULT_DATA
TAILNET_ORIGIN = None
SAM = None
TEAM = None
WRITER_HANDLE = None


def stamp():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def write_json(path, value):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, indent=2), encoding="utf-8")
    os.replace(temp, path)


def state():
    path = DATA / "state.json"
    return json.loads(path.read_text("utf-8")) if path.exists() else {"batches": [], "active_batch": None, "active_sample": 0, "reviewer": ""}


def batch_path(bid):
    if not re.fullmatch(r"[a-f0-9]{32}", bid):
        raise ValueError("Invalid batch identity")
    return DATA / "batches" / bid


def read_batch(bid):
    load = lambda: json.loads((batch_path(bid) / "batch.json").read_text("utf-8"))
    return TEAM.batch(bid, load) if TEAM else load()


def working_bytes(bid, index):
    load = lambda: (batch_path(bid) / str(index) / "working.png").read_bytes()
    return TEAM.mask(bid, int(index), load) if TEAM else load()


class TeamBatchFiles:
    """Read-only file view for SAM's synchronous source snapshot."""
    def __init__(self, bid, parts=()):
        self.bid, self.parts = bid, parts

    def __truediv__(self, part):
        return TeamBatchFiles(self.bid,self.parts+(str(part),))

    def read_bytes(self):
        if len(self.parts) == 2 and self.parts[1] == 'working.png':
            return working_bytes(self.bid,int(self.parts[0]))
        return batch_path(self.bid).joinpath(*self.parts).read_bytes()


def safe_member(name):
    p = PurePosixPath(name)
    if not name or "\\" in name or ":" in name or p.is_absolute() or ".." in p.parts or str(p) != name or len(name) > 250:
        raise ValueError("Unsafe archive path")
    return name


def decode_label(raw, size=None):
    im = Image.open(io.BytesIO(raw))
    if im.format != "PNG" or im.mode != "L":
        raise ValueError("Masks must be single-channel 8-bit grayscale PNGs")
    im.load()
    if size and im.size != size:
        raise ValueError("Mask dimensions must match the native image")
    if im.width * im.height > MAX_PIXELS or max(im.getdata()) > 4:
        raise ValueError("Mask uses unsupported class IDs or exceeds the pixel limit")
    return im


def import_zip(raw):
    if len(raw) > MAX_ZIP:
        raise ValueError("ZIP exceeds 100 MiB")
    z = zipfile.ZipFile(io.BytesIO(raw))
    members = z.infolist()
    if len(members) > 500 or sum(x.file_size for x in members) > MAX_UNPACKED:
        raise ValueError("Archive exceeds the expanded size or entry limit")
    names = set()
    for info in members:
        safe_member(info.filename.rstrip("/") if info.is_dir() else info.filename)
        if info.filename in names or info.flag_bits & 1 or (info.external_attr >> 16) & 0o170000 == 0o120000:
            raise ValueError("Duplicate, encrypted or linked archive entries are not accepted")
        names.add(info.filename)
    if "manifest.json" not in names:
        raise ValueError("ZIP needs manifest.json at its root")
    if z.getinfo("manifest.json").file_size > 1024 * 1024:
        raise ValueError("Manifest exceeds 1 MiB")
    manifest = json.loads(z.read("manifest.json"))
    if manifest.get("taxonomy") != TAXONOMY or manifest.get("classes") != CLASSES:
        raise ValueError("Taxonomy must be v6-object-5 with the exact IDs 0..4 mapping")
    samples = manifest.get("samples")
    if not isinstance(samples, list) or not 1 <= len(samples) <= 100:
        raise ValueError("Batch must contain between 1 and 100 samples")
    assignments = {}
    for existing in state()["batches"]:
        for s in read_batch(existing["id"])["samples"]:
            assignments[("session", s["dataset"], s["session"])] = s["split"]
            assignments[("run", s["dataset"], s["run"])] = s["split"]
    prepared, seen, pixels = [], set(), 0
    for s in samples:
        if not isinstance(s, dict):
            raise ValueError("Invalid sample record")
        for field in ("id", "image", "suggestion", "dataset", "session", "run", "split", "model"):
            if not isinstance(s.get(field), str) or not s[field].strip() or len(s[field]) > 200:
                raise ValueError("Each sample needs bounded text metadata: " + field)
        if s["id"] in seen:
            raise ValueError("Sample identities must be unique")
        seen.add(s["id"])
        for key in (("session", s["dataset"], s["session"]), ("run", s["dataset"], s["run"])):
            if key in assignments and assignments[key] != s["split"]:
                raise ValueError("Conflicting split for the same dataset/session or dataset/run")
            assignments[key] = s["split"]
        image_raw, suggestion_raw = z.read(safe_member(s["image"])), z.read(safe_member(s["suggestion"]))
        im = Image.open(io.BytesIO(image_raw))
        if im.format not in ("PNG", "JPEG") or getattr(im, "n_frames", 1) != 1:
            raise ValueError("Images must be static PNG or JPEG")
        im.load()
        pixels += im.width * im.height
        if im.width * im.height > MAX_PIXELS or pixels > 100_000_000:
            raise ValueError("Image pixel limit exceeded")
        decode_label(suggestion_raw, im.size)
        working_raw = z.read(safe_member(s["mask"])) if s.get("mask") else suggestion_raw
        decode_label(working_raw, im.size)
        conflict_raw = None
        if s.get("conflict_map"):
            conflict_raw = z.read(safe_member(s["conflict_map"]))
            if s.get("conflict_sha256") and hashlib.sha256(conflict_raw).hexdigest() != s["conflict_sha256"]:
                raise ValueError("Conflict bitmap hash does not match archive bytes")
            conflict = Image.open(io.BytesIO(conflict_raw))
            conflict.load()
            if (conflict.format != "PNG" or conflict.mode != "L" or conflict.size != im.size
                    or not set(conflict.getdata()) <= {0, 1, 255}
                    or sum(v != 0 for v in conflict.getdata()) != s.get("conflict_pixels")):
                raise ValueError("Invalid SAM conflict map")
        assistance_assets = {}
        if s.get("assistance_assets"):
            if not isinstance(s["assistance_assets"], dict) or set(s["assistance_assets"]) != {"base_mask", "raw_mask"}:
                raise ValueError("Invalid assistance assets")
            for key, asset in s["assistance_assets"].items():
                blob = z.read(safe_member(asset["path"]))
                if hashlib.sha256(blob).hexdigest() != asset["sha256"]:
                    raise ValueError("Assistance snapshot hash mismatch")
                decode_label(blob, im.size)
                assistance_assets[key] = blob
        for key, content in (("image_sha256", image_raw), ("suggestion_sha256", suggestion_raw), ("mask_sha256", working_raw)):
            if s.get(key) and s[key] != hashlib.sha256(content).hexdigest():
                raise ValueError("Source hash does not match archive bytes")
        if (s.get("width") and s["width"] != im.width) or (s.get("height") and s["height"] != im.height):
            raise ValueError("Declared dimensions do not match native image")
        # Imported decisions remain provenance; approval always requires a local review.
        record = {k: s[k] for k in ("id", "dataset", "session", "run", "split", "model")}
        record.update({"filename": str(s.get("filename", PurePosixPath(s["image"]).name))[:200], "width": im.width, "height": im.height,
                       "state": "edited" if s.get("mask") else "pending", "revision": 0, "reviewer": "", "note": "",
                       "history": [], "provenance": s, "image_sha256": hashlib.sha256(image_raw).hexdigest(),
                       "suggestion_sha256": hashlib.sha256(suggestion_raw).hexdigest()})
        prepared.append((record, image_raw, suggestion_raw, working_raw, im.convert("RGB"), conflict_raw, assistance_assets))
    bid = uuid.uuid4().hex
    folder = batch_path(bid)
    folder.mkdir(parents=True)
    try:
        records = []
        for index, (record, image_raw, suggestion_raw, working_raw, rgb, conflict_raw, assistance_assets) in enumerate(prepared):
            target = folder / str(index)
            target.mkdir()
            (target / "original").write_bytes(image_raw)
            (target / "suggestion.png").write_bytes(suggestion_raw)
            (target / "working.png").write_bytes(working_raw)
            if conflict_raw:
                (target / "conflicts.png").write_bytes(conflict_raw)
            for key, blob in assistance_assets.items():
                (target / (key + ".png")).write_bytes(blob)
            rgb.thumbnail((180, 110))
            rgb.save(target / "thumb.jpg", quality=85)
            records.append(record)
        batch = {"id": bid, "title": str(manifest.get("title", "Imported batch"))[:120], "imported_at": stamp(),
                 "source_manifest": manifest, "samples": records}
        write_json(folder / "batch.json", batch)
        current = state()
        current["batches"].append({"id": bid, "title": batch["title"]})
        current.update(active_batch=bid, active_sample=0)
        write_json(DATA / "state.json", current)
    except Exception:
        shutil.rmtree(folder)
        raise
    return snapshot()


def snapshot():
    current = state()
    current["token"] = TOKEN
    current["classes"] = CLASSES
    current["sam3"] = SAM.listing() if SAM else {"enabled": False, "jobs": []}
    current["batch"] = read_batch(current["active_batch"]) if current["active_batch"] else None
    current["review_history"], current["undo_review"] = review_history(current["batch"])
    return current


def review_history(batch):
    """Expose local audit events; imported history is provenance, never actionable."""
    if not batch:
        return [], None
    events, eligible = [], []
    for index, sample in enumerate(batch["samples"]):
        for position, event in enumerate(sample["history"]):
            if event["action"] not in ("approved", "rejected", "skipped", "undo_review"):
                continue
            row = dict(event, event_id=event.get("event_id", f"{index}:{position}"), index=index, filename=sample["filename"])
            events.append(row)
            if (position == len(sample["history"]) - 1 and sample["state"] == event["action"]
                    and event["action"] != "undo_review" and sample["revision"] == event["revision"]):
                eligible.append(row)
    key = lambda event: (event.get("sequence", 0), event["at"], event["index"])
    events.sort(key=key, reverse=True)
    eligible.sort(key=key, reverse=True)
    return events, eligible[0] if eligible else None


def append_event(batch, sample, event):
    sequence = max((e.get("sequence", 0) for s in batch["samples"] for e in s["history"]), default=0) + 1
    sample["history"].append(dict(event, event_id=uuid.uuid4().hex, sequence=sequence))


def export_batch(bid):
    batch = read_batch(bid)
    manifest = {"format": "mask-review-v1", "taxonomy": TAXONOMY, "title": batch["title"], "classes": CLASSES, "exported_at": stamp(),
                "intended_use": "human-reviewed object labels; downstream datasets need an explicit adapter and validation",
                "source_manifest": batch["source_manifest"], "samples": [], "decisions": []}
    accepted = sum(s["state"] == "approved" for s in batch["samples"])
    if not accepted:
        raise ValueError("Approve at least one mask before exporting")
    content = io.BytesIO()
    with zipfile.ZipFile(content, "w", zipfile.ZIP_DEFLATED) as z:
        for i, s in enumerate(batch["samples"]):
            manifest["decisions"].append({k: s[k] for k in ("id", "state", "revision", "reviewer", "note", "history")})
            if s["state"] != "approved":
                continue
            folder = batch_path(bid) / str(i)
            for name, hash_key in (("original", "image_sha256"), ("suggestion.png", "suggestion_sha256")):
                if hashlib.sha256((folder / name).read_bytes()).hexdigest() != s[hash_key]:
                    raise ValueError("Immutable source integrity failed")
            mask_bytes = working_bytes(bid, i)
            decode_label(mask_bytes, (s["width"], s["height"]))
            mask_hash = hashlib.sha256(mask_bytes).hexdigest()
            last = s["history"][-1] if s["history"] else {}
            if last.get("action") != "approved" or last.get("revision") != s["revision"] or last.get("mask_sha256") != mask_hash:
                raise ValueError("Approved mask integrity failed; review and approve this revision again")
            record = {k: s[k] for k in ("id", "dataset", "session", "run", "split", "model")}
            record.update(image=f"originals/{i}.bin", filename=s["filename"], suggestion=f"suggestions/{i}.png", provenance=s["provenance"],
                          image_sha256=s["image_sha256"], suggestion_sha256=s["suggestion_sha256"], width=s["width"], height=s["height"], mask_sha256=mask_hash)
            z.write(folder / "original", record["image"])
            z.write(folder / "suggestion.png", record["suggestion"])
            for key in ("sam_assistance", "sam_queries", "sam_job_id"):
                if key in s["provenance"]:
                    record[key] = s["provenance"][key]
            if s["provenance"].get("assistance_assets"):
                record["assistance_assets"] = {}
                for key, asset in s["provenance"]["assistance_assets"].items():
                    blob = (folder / (key + ".png")).read_bytes()
                    if hashlib.sha256(blob).hexdigest() != asset["sha256"]:
                        raise ValueError("Immutable assistance snapshot integrity failed")
                    target = f"assistance/{i}-{key}.png"
                    z.writestr(target, blob)
                    record["assistance_assets"][key] = dict(path=target, sha256=asset["sha256"])
            if (folder / "conflicts.png").exists():
                record.update(conflict_map=f"conflicts/{i}.png", conflict_pixels=s["provenance"]["conflict_pixels"])
                if s["provenance"].get("sam_job_id"):
                    record["sam_job_id"] = s["provenance"]["sam_job_id"]
                conflict_raw = (folder / "conflicts.png").read_bytes()
                conflict_sha = hashlib.sha256(conflict_raw).hexdigest()
                if s["provenance"].get("conflict_sha256") and conflict_sha != s["provenance"]["conflict_sha256"]:
                    raise ValueError("Immutable conflict bitmap integrity failed")
                record["conflict_sha256"] = conflict_sha
                z.write(folder / "conflicts.png", record["conflict_map"])
            if s["state"] == "approved":
                record["mask"] = f"accepted/{i}.png"
                z.writestr(record["mask"], mask_bytes)
            if TEAM:
                record["review_history"] = s["history"]
                record["mask_revisions"] = TEAM.revisions(bid, i)
            manifest["samples"].append(record)
        if TEAM:
            manifest["reviewers"] = TEAM.members()
        z.writestr("manifest.json", json.dumps(manifest, indent=2))
    path = DATA.parent / "exports"
    path.mkdir(exist_ok=True)
    name = "approved-" + time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8] + ".zip"
    with (path / name).open("xb") as saved:
        saved.write(content.getvalue())
    return content.getvalue(), name


class ReviewServer(ThreadingHTTPServer):
    # Tailscale's proxy opens concurrent thumbnail connections for the queue.
    # The default small TCP backlog can reject bursts before a handler runs.
    request_queue_size = 64


class Handler(BaseHTTPRequestHandler):
    def team_session(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get('Cookie', ''))
            # The shared SQLite connection is used only under the same lock as
            # mutations, including authorization before a POST body is read.
            with LOCK:
                return TEAM.authenticate(cookie['review_session'].value if 'review_session' in cookie else None)
        except Exception:
            return None

    def team_snapshot(self, session):
        result = snapshot()
        bid = session['batch'] or result['active_batch']
        if bid not in [b['id'] for b in result['batches']]:
            bid = None
        result.update(active_batch=bid, active_sample=session['idx'], reviewer=session['name'], token=session['csrf'], team=TEAM.team(session))
        result['batch'] = read_batch(bid) if bid else None
        result['review_history'], result['undo_review'] = review_history(result['batch'])
        if session['role'] != 'owner':
            result['sam3'] = {'enabled': False, 'jobs': []}
        return result

    def team_post(self, raw, session):
        path = urlparse(self.path).path
        if path == '/api/team/login':
            sid = TEAM.login(json.loads(raw).get('code'))
            session = TEAM.authenticate(sid)
            flags = '; Secure' if TAILNET_ORIGIN and self.headers.get('Origin') == TAILNET_ORIGIN else ''
            return self.send(200, dict(enabled=True, authenticated=True, user=TEAM.team(session)['user'], token=session['csrf']),
                             extra={'Set-Cookie': f'review_session={sid}; Path=/; HttpOnly; SameSite=Strict; Max-Age={TEAM.session_ttl}{flags}'})
        owner_paths = ('/api/import','/api/export','/api/team/invite','/api/team/revoke')
        if (path in owner_paths or path.startswith('/api/sam/')) and session['role'] != 'owner':
            raise TeamError('Owner access required',403)
        if path == '/api/import':
            result = import_zip(raw)
            TEAM.cursor(session,result['active_batch'],0)
            return self.send(200,self.team_snapshot(session))
        request = json.loads(raw)
        if not isinstance(request,dict):
            raise TeamError('A JSON object is required',400)
        if path == '/api/team/logout':
            TEAM.logout(session)
            return self.send(200, {'enabled':True,'authenticated':False},extra={'Set-Cookie':'review_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0'})
        if path == '/api/team/invite':
            return self.send(200, TEAM.invite(request.get('name'),request.get('role','reviewer')))
        if path == '/api/team/revoke':
            TEAM.revoke(request.get('user_id'),session)
            return self.send(200,{'members':TEAM.members()})
        if path == '/api/session':
            current = self.team_snapshot(session)
            bid = request.get('active_batch',current['active_batch'])
            if bid not in [b['id'] for b in current['batches']] and bid is not None:
                raise TeamError('Unknown batch',400)
            index = int(request.get('active_sample',current['active_sample']))
            if bid and not 0 <= index < len(read_batch(bid)['samples']):
                raise TeamError('Unknown sample',400)
            TEAM.cursor(session,bid,index)
            return self.send(200,self.team_snapshot(session))
        if path.startswith('/api/sam/'):
            return self.team_sam(request, session, path)
        bid = request['batch']
        batch = read_batch(bid)
        if path == '/api/export':
            data,name = export_batch(bid)
            return self.send(200,data,'application/zip',{'Content-Disposition':f'attachment; filename="{name}"'})
        if path == '/api/team/next':
            TEAM.db.execute('DELETE FROM claims WHERE session_id=?',(session['id'],))
            TEAM.db.commit()
            index = None
            for candidate,sample in enumerate(batch['samples']):
                if sample['state'] in ('approved','rejected','skipped'):
                    continue
                try:
                    TEAM.claim(session,bid,candidate)
                    index = candidate
                    TEAM.cursor(session,bid,index)
                    break
                except TeamError:
                    continue
            return self.send(200,dict(TEAM.team(session),index=index))
        if path == '/api/undo_review':
            _,candidate = review_history(batch)
            if not candidate or candidate['event_id'] != request.get('event_id'):
                raise TeamError('That review can no longer be undone')
            index = candidate['index']
        else:
            index = int(request['index'])
        if not 0 <= index < len(batch['samples']):
            raise TeamError('Unknown sample',400)
        if path in ('/api/team/claim','/api/team/release'):
            TEAM.claim(session,bid,index,path.endswith('/release'))
            return self.send(200, TEAM.team(session))
        if path not in ('/api/mask','/api/note','/api/decision','/api/undo_review','/api/team/restore'):
            return self.send(404,{'error':'Not found'})
        TEAM.require_claim(session,bid,index)
        sample = batch['samples'][index]
        if request.get('revision') != sample['revision']:
            raise TeamError('This frame changed elsewhere. Reload before continuing.')
        before = working_bytes(bid,index)
        after = before
        action = 'note'
        details = {}
        if path == '/api/mask':
            pixels = base64.b64decode(request['pixels'],validate=True)
            if len(pixels) != sample['width']*sample['height'] or max(pixels)>4:
                raise TeamError('Working pixels must match native dimensions and class IDs',400)
            action = request.get('reason','brush')
            if action not in ('brush','erase','undo','redo','reset'):
                raise TeamError('Unknown edit action',400)
            buffer = io.BytesIO()
            Image.frombytes('L',(sample['width'],sample['height']),pixels).save(buffer,format='PNG')
            after = buffer.getvalue()
        elif path == '/api/team/restore':
            after = TEAM.restore(bid,index,request['snapshot_id'])
            decode_label(after,(sample['width'],sample['height']))
            action = 'restore'
            details['snapshot_id'] = request['snapshot_id']
        elif path == '/api/decision':
            action = request['decision']
            if action not in ('approved','rejected','skipped'):
                raise TeamError('Unknown decision',400)
            sample['note'] = str(request.get('note',sample['note']))[:500]
        elif path == '/api/undo_review':
            action = 'undo_review'
            details.update(undone_event_id=candidate['event_id'],undone_action=candidate['action'])
        else:
            sample['note'] = str(request.get('note',''))[:500]
        sample.update(revision=sample['revision']+1,state=action if action in ('approved','rejected','skipped') else 'edited',
                      reviewer=session['name'] if action in ('approved','rejected','skipped') else '')
        append_event(batch,sample,dict(at=stamp(),action=action,revision=sample['revision'],reviewer=session['name'],
                     user_id=session['user_id'],note=sample['note'],mask_sha256=hashlib.sha256(after).hexdigest(),**details))
        TEAM.save(bid,index,batch,before,after,session,action,stamp())
        if path == '/api/undo_review':
            TEAM.cursor(session,bid,index)
            return self.send(200,self.team_snapshot(session))
        return self.send(200,sample)

    def team_sam(self, request, session, path):
        if not SAM or not SAM.enabled:
            raise TeamError('Model generation is unavailable. Import a suggestion ZIP for human review.',400)
        if path == '/api/sam/cancel':
            return self.send(200,SAM.cancel(request['job_id']))
        if path == '/api/sam/generate':
            bid = request['batch']
            if request.get('retry_of') and any(key in request for key in ('profile','assistance')):
                raise TeamError('Retry uses frozen settings',400)
            assist = request.get('assistance')
            if assist is not None:
                if not isinstance(assist,dict) or session['batch'] != bid or session['idx'] != assist.get('index'):
                    raise TeamError('The open frame changed. Reload and draw a fresh box.',400)
                TEAM.require_claim(session,bid,assist['index'])
            return self.send(200,SAM.create(read_batch(bid),TeamBatchFiles(bid),request.get('retry_of'),request.get('profile'),assist))
        if path == '/api/sam/import':
            job = request['job_id']
            existing = SAM.imported(job)
            if existing:
                current = state()
                if existing not in [b['id'] for b in current['batches']]:
                    recovered = read_batch(existing)
                    current['batches'].append({'id':existing,'title':recovered['title']})
                    write_json(DATA/'state.json',current)
                return self.send(200,{'imported_batch':existing,'already_imported':True})
            result = import_zip(SAM.result_zip(job,decode_label))
            return self.send(200,{'imported_batch':result['active_batch'],'already_imported':False})
        return self.send(404,{'error':'Not found'})

    def log_message(self, fmt, *args):
        print(fmt % args)

    def send(self, status, body, kind="application/json", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' blob:; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def host_ok(self):
        allowed = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
        if TAILNET_ORIGIN:
            allowed.add(urlparse(TAILNET_ORIGIN).netloc)
        return self.headers.get("Host") in allowed

    def do_GET(self):
        try:
            if not self.host_ok():
                return self.send(403, {"error": "Configured review address required"})
            path = urlparse(self.path).path
            with LOCK:
                if path == '/api/team/status':
                    session = self.team_session() if TEAM else None
                    result = {'enabled':bool(TEAM),'authenticated':bool(session)}
                    if session:
                        result['user'] = TEAM.team(session)['user']
                    return self.send(200,result)
                if TEAM:
                    session = self.team_session()
                    if not session and path not in ('/','/app.js','/style.css'):
                        return self.send(401,{'error':'Sign in to review'})
                    if path == '/api/state':
                        return self.send(200,self.team_snapshot(session))
                    if path == '/api/team/members':
                        if session['role'] != 'owner':
                            return self.send(403,{'error':'Owner access required'})
                        return self.send(200,{'members':TEAM.members()})
                    revisions = re.fullmatch(r'/api/team/revisions/([a-f0-9]{32})/(\d+)',path)
                    if revisions:
                        bid,index = revisions.groups()
                        read_batch(bid)['samples'][int(index)]
                        return self.send(200,{'revisions':TEAM.revisions(bid,int(index))})
                    if path == '/api/sam/jobs' and session['role'] != 'owner':
                        return self.send(403,{'error':'Owner access required'})
                if path == "/api/library":
                    return self.send(200, read_library(DATA))
                if path == "/api/state":
                    return self.send(200, snapshot())
                if path == "/api/sam/jobs":
                    return self.send(200, SAM.listing() if SAM else {"enabled": False, "jobs": []})
                history_match = re.fullmatch(r"/api/history/([a-f0-9]{32})", path)
                if history_match:
                    events, undo = review_history(read_batch(history_match.group(1)))
                    return self.send(200, {"review_history": events, "undo_review": undo})
                if path == "/demo.zip":
                    demo = DATA.parent / "fixtures" / "synthetic-demo.zip"
                    if demo.exists():
                        return self.send(200, demo.read_bytes(), "application/zip")
                match = re.fullmatch(r"/asset/([a-f0-9]{32})/(\d+)/(image|suggestion|working|thumb|pixels|suggestion_pixels|conflicts)", path)
                if match:
                    bid, index, kind = match.groups()
                    s = read_batch(bid)["samples"][int(index)]
                    if kind in ("pixels", "suggestion_pixels"):
                        target = "working.png" if kind == "pixels" else "suggestion.png"
                        raw = working_bytes(bid,index) if kind == 'pixels' else (batch_path(bid)/index/target).read_bytes()
                        labels = decode_label(raw, (s["width"], s["height"]))
                        return self.send(200, labels.tobytes(), "application/octet-stream")
                    name = {"image": "original", "suggestion": "suggestion.png", "working": "working.png", "thumb": "thumb.jpg", "conflicts": "conflicts.png"}[kind]
                    if TEAM and kind == 'working':
                        return self.send(200,working_bytes(bid,index),'image/png')
                    mime = "image/jpeg" if kind == "thumb" else "image/png"
                    if kind == "image":
                        mime = "image/" + Image.open(batch_path(bid) / index / name).format.lower().replace("jpg", "jpeg")
                    return self.send(200, (batch_path(bid) / index / name).read_bytes(), mime)
                files = {"/": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "text/javascript"), "/style.css": ("style.css", "text/css")}
                if path in files:
                    name, mime = files[path]
                    return self.send(200, (ROOT / name).read_bytes(), mime)
                self.send(404, {"error": "Not found"})
        except Exception:
            self.send(404, {"error": "Not found"})

    def do_POST(self):
        try:
            origin = self.headers.get("Origin")
            allowed = (f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}")
            if TAILNET_ORIGIN:
                allowed += (TAILNET_ORIGIN,)
            session = self.team_session() if TEAM else None
            login = TEAM and urlparse(self.path).path == '/api/team/login'
            if not self.host_ok() or (origin and origin not in allowed) or self.headers.get("Sec-Fetch-Site") == "cross-site":
                return self.send(403, {"error": "Local session authorization required"})
            if TEAM and not login and not session:
                return self.send(401,{'error':'Sign in to review'})
            expected = session['csrf'] if TEAM and session else TOKEN
            if not login and self.headers.get('X-Review-Token') != expected:
                return self.send(403,{'error':'Session authorization required'})
            length = int(self.headers.get("Content-Length", "0"))
            limit = MAX_ZIP if self.path == "/api/import" else 18 * 1024 * 1024
            if not 0 < length <= limit:
                return self.send(413, {"error": "Request exceeds the size limit"})
            raw = self.rfile.read(length)
            with LOCK:
                if TEAM:
                    # Revocation may have happened while this request waited
                    # for another mutation or finished reading its body.
                    if not login:
                        session = self.team_session()
                        if not session:
                            return self.send(401,{'error':'Sign in to review'})
                        if self.headers.get('X-Review-Token') != session['csrf']:
                            return self.send(403,{'error':'Session authorization required'})
                    return self.team_post(raw,session)
                if self.path == "/api/import":
                    return self.send(200, import_zip(raw))
                request = json.loads(raw)
                if self.path.startswith("/api/sam/"):
                    if not SAM or not SAM.enabled:
                        raise ValueError("Model generation is unavailable. Import a suggestion ZIP for human review.")
                    if self.path == "/api/sam/generate":
                        bid = request["batch"]
                        if request.get("retry_of") and any(key in request for key in ("profile", "assistance")):
                            raise ValueError("Retry uses frozen settings; do not supply profile or assistance")
                        if request.get("assistance") is not None:
                            active = state()
                            assist = request["assistance"]
                            if (not isinstance(assist, dict) or active["active_batch"] != bid
                                    or active["active_sample"] != assist.get("index")):
                                raise ValueError("The open frame changed. Reload and draw a fresh box.")
                        return self.send(200, SAM.create(read_batch(bid), batch_path(bid), request.get("retry_of"),
                                                       request.get("profile"), request.get("assistance")))
                    if self.path == "/api/sam/cancel":
                        return self.send(200, SAM.cancel(request["job_id"]))
                    if self.path == "/api/sam/import":
                        job_id = request["job_id"]
                        existing = SAM.imported(job_id)
                        if existing:
                            current = state()
                            if existing not in [b["id"] for b in current["batches"]]:
                                recovered = read_batch(existing)
                                current["batches"].append({"id": existing, "title": recovered["title"]})
                                write_json(DATA / "state.json", current)
                            return self.send(200, {"imported_batch": existing, "already_imported": True})
                        result = import_zip(SAM.result_zip(job_id, decode_label))
                        return self.send(200, {"imported_batch": result["active_batch"], "already_imported": False})
                    return self.send(404, {"error": "Not found"})
                if self.path == "/api/session":
                    current = state()
                    reviewer = str(request.get("reviewer", current["reviewer"])).strip()[:100]
                    current["reviewer"] = reviewer
                    bid = request.get("active_batch", current["active_batch"])
                    if bid and bid not in [b["id"] for b in current["batches"]]:
                        raise ValueError("Unknown batch")
                    current["active_batch"] = bid
                    index = int(request.get("active_sample", current["active_sample"]))
                    if bid and not 0 <= index < len(read_batch(bid)["samples"]):
                        raise ValueError("Unknown sample")
                    current["active_sample"] = index
                    write_json(DATA / "state.json", current)
                    return self.send(200, snapshot())
                bid = request["batch"]
                if self.path == "/api/export":
                    raw_zip, name = export_batch(bid)
                    return self.send(200, raw_zip, "application/zip", {"Content-Disposition": f'attachment; filename="{name}"'})
                batch = read_batch(bid)
                if self.path == "/api/undo_review":
                    current = state()
                    _, candidate = review_history(batch)
                    if (current["active_batch"] != bid or not candidate or request.get("event_id") != candidate["event_id"]
                            or request.get("revision") != candidate["revision"]):
                        return self.send(409, {"error": "That review can no longer be undone. Reload the history and try again."})
                    index = candidate["index"]
                    s = batch["samples"][index]
                    had_edits = bool(s["provenance"].get("mask")) or any(e["action"] in ("brush", "erase", "undo", "redo", "reset") for e in s["history"])
                    s.update(state="edited" if had_edits else "pending", reviewer="", revision=s["revision"] + 1)
                    append_event(batch, s, {"at": stamp(), "action": "undo_review", "revision": s["revision"],
                                           "reviewer": current["reviewer"], "undone_event_id": candidate["event_id"],
                                           "undone_action": candidate["action"]})
                    write_json(batch_path(bid) / "batch.json", batch)
                    current["active_sample"] = index
                    write_json(DATA / "state.json", current)
                    return self.send(200, snapshot())
                index = int(request["index"])
                if not 0 <= index < len(batch["samples"]):
                    raise ValueError("Unknown sample")
                s = batch["samples"][index]
                if request.get("revision") != s["revision"]:
                    return self.send(409, {"error": "This frame changed elsewhere. Reload before continuing."})
                if self.path == "/api/mask":
                    pixels = base64.b64decode(request["pixels"], validate=True)
                    if len(pixels) != s["width"] * s["height"] or max(pixels) > 4:
                        raise ValueError("Working pixels must match native dimensions and class IDs")
                    reason = request.get("reason", "brush")
                    if reason not in ("brush", "erase", "undo", "redo", "reset"):
                        raise ValueError("Unknown edit action")
                    image = Image.frombytes("L", (s["width"], s["height"]), pixels)
                    # Persist loss of approval first. A crash cannot leave changed approved labels.
                    s.update(state="edited", reviewer="", revision=s["revision"] + 1)
                    append_event(batch, s, {"at": stamp(), "action": reason, "revision": s["revision"], "reviewer": state()["reviewer"]})
                    write_json(batch_path(bid) / "batch.json", batch)
                    target = batch_path(bid) / str(index) / "working.png"
                    temp = target.with_suffix(".tmp")
                    image.save(temp, format="PNG")
                    os.replace(temp, target)
                elif self.path == "/api/decision":
                    decision = request["decision"]
                    reviewer = str(request.get("reviewer", "")).strip()
                    if decision not in ("approved", "rejected", "skipped") or not reviewer or len(reviewer) > 100:
                        raise ValueError("Enter a reviewer name before recording a decision")
                    s.update(state=decision, reviewer=reviewer, note=str(request.get("note", ""))[:500])
                    append_event(batch, s, {"at": stamp(), "action": decision, "revision": s["revision"], "reviewer": reviewer, "note": s["note"],
                                         "mask_sha256": hashlib.sha256((batch_path(bid) / str(index) / "working.png").read_bytes()).hexdigest()})
                elif self.path == "/api/note":
                    s["note"] = str(request.get("note", ""))[:500]
                else:
                    return self.send(404, {"error": "Not found"})
                write_json(batch_path(bid) / "batch.json", batch)
                return self.send(200, s)
        except TeamError as e:
            self.send(e.status,{'error':str(e)})
        except (ValueError, KeyError, TypeError, IndexError, zipfile.BadZipFile, OSError, Image.DecompressionBombError) as e:
            self.send(400, {"error": str(e)[:200]})


def acquire_writer(data):
    """OS-held lock, released on process exit, shared by both launch modes."""
    global WRITER_HANDLE
    WRITER_HANDLE = (data/'writer.lock').open('a+b')
    WRITER_HANDLE.seek(0)
    if os.name == 'nt':
        import msvcrt
        if WRITER_HANDLE.read(1) == b'':
            WRITER_HANDLE.write(b'0')
            WRITER_HANDLE.flush()
        WRITER_HANDLE.seek(0)
        msvcrt.locking(WRITER_HANDLE.fileno(),msvcrt.LK_NBLCK,1)
    else:
        import fcntl
        fcntl.flock(WRITER_HANDLE,fcntl.LOCK_EX|fcntl.LOCK_NB)


def main():
    global DATA, TAILNET_ORIGIN, SAM, TEAM
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument('--team',action='store_true',help='Require private team sign-in and collaboration')
    parser.add_argument("--tailnet-origin", help="Exact HTTPS Tailscale Serve origin, e.g. https://machine.tail123.ts.net:8766")
    args = parser.parse_args()
    if args.tailnet_origin:
        origin = urlparse(args.tailnet_origin)
        if (origin.scheme != "https" or not origin.hostname or not origin.hostname.endswith(".ts.net")
                or origin.username or origin.password or origin.path or origin.query or origin.fragment):
            parser.error("--tailnet-origin must be an HTTPS .ts.net origin with no path or credentials")
        TAILNET_ORIGIN = args.tailnet_origin
    DATA = args.data.resolve()
    DATA.mkdir(parents=True, exist_ok=True)
    try:
        acquire_writer(DATA)
    except OSError:
        parser.error('Another review writer already owns this data directory')
    if args.team:
        TEAM = TeamStore(DATA)
        if not TEAM.members():
            parser.error('Initialize the owner with team_admin.py before starting --team')
    SAM = None  # Suggestions can be imported as review ZIPs; no model worker is bundled.
    server = ReviewServer(("127.0.0.1", args.port), Handler)
    print(f"Mask Review: http://127.0.0.1:{args.port}\nLocal storage: {DATA}", flush=True)
    if TAILNET_ORIGIN:
        print(f"Configured Tailscale address: {TAILNET_ORIGIN}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
