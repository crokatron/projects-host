"""Owner project workspaces; the coding container never receives NAS credentials."""
import base64, difflib, hashlib, http.client, json, os, re, shutil, threading, time, uuid
from pathlib import Path

STUDIO_LOCK=threading.RLock()
STUDIO_EXT={'.html','.js','.css','.py','.json','.svg','.png','.jpg','.webp','.txt','.md','.wasm'}

def studio_bridge(path,body=None):
    c=http.client.HTTPConnection('mungoon-codex',9080,timeout=55)
    try:
        raw=json.dumps(body).encode() if body is not None else None
        c.request('POST' if body is not None else 'GET',path,raw,{'Content-Type':'application/json'})
        r=c.getresponse();data=json.loads(r.read(20000001))
        if r.status!=200:raise ValueError(data.get('error','Codex is unavailable.'))
        return data
    except (OSError,TimeoutError,http.client.HTTPException):raise ValueError('The NAS coding service is starting. Try again shortly.') from None
    finally:c.close()

class Studio:
    def __init__(self,root,data):
        self.root=Path(root);self.data=Path(data);self.work=self.data/'studio-workspaces';self.meta=self.data/'private/studio'
    def paths(self,sid):
        if not isinstance(sid,str) or not re.fullmatch('[a-f0-9]{32}',sid):raise ValueError('Invalid workspace.')
        folder=self.work/sid
        if folder.is_symlink() or not folder.is_dir():raise ValueError('Workspace not found.')
        return folder,self.meta/(sid+'.json')
    def listing(self):
        rows=[]
        for p in self.meta.glob('*.json'):
            doc=json.loads(p.read_text());rows.append({k:doc[k] for k in ('id','project','created')})
        return sorted(rows,key=lambda x:x['created'],reverse=True)
    def create(self,project):
        from history import GROUPS,tool_files
        if project not in GROUPS:raise ValueError('Choose a project.')
        files=tool_files(self.root,project)
        if not files:raise ValueError('Project is not published yet.')
        if len(self.listing())>=40:raise ValueError('Workspace limit reached. Reuse an existing workspace.')
        sid=uuid.uuid4().hex;folder=self.work/sid;folder.mkdir(parents=True)
        baseline={}
        for name in files:
            raw=(self.root/name).read_bytes();target=folder/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(raw)
            baseline[name]=hashlib.sha256(raw).hexdigest()
        (folder/'AGENTS.md').write_text('This is an editable copy of a published Mungoon tool. Edit files only under '+', '.join(GROUPS[project])+'. Preserve API paths and NAS draft saving. Do not modify authentication, read secrets or deploy. The owner reviews changes and publishes through Project Studio. Do not claim tests or publishing succeeded unless verified.\n')
        self.meta.mkdir(parents=True,exist_ok=True,mode=0o700)
        doc={'id':sid,'project':project,'created':time.strftime('%Y-%m-%d %H:%M:%S UTC',time.gmtime()),'baseline':baseline}
        (self.meta/(sid+'.json')).write_text(json.dumps(doc))
        return {k:doc[k] for k in ('id','project','created')}
    def review(self,sid,include_files=False):
        from history import GROUPS
        folder,meta=self.paths(sid);doc=json.loads(meta.read_text());prefixes=GROUPS[doc['project']]
        after={};rows=[];total=0
        for p in folder.rglob('*'):
            if p.is_symlink():raise ValueError('Symbolic links are not allowed in project changes.')
            if not p.is_file():continue
            name=p.relative_to(folder).as_posix()
            if not any(name.startswith(prefix) for prefix in prefixes):continue
            if any(x.startswith('.') or x=='__pycache__' for x in p.relative_to(folder).parts):continue
            if p.suffix.lower() not in STUDIO_EXT:raise ValueError('Unsupported project file: '+name)
            raw=p.read_bytes();total+=len(raw)
            if total>40000000:raise ValueError('Project changes are too large.')
            after[name]=raw
        names=set(doc['baseline'])|set(after);fingerprint={}
        for name in sorted(names):
            raw=after.get(name);digest=hashlib.sha256(raw).hexdigest() if raw is not None else None
            if digest==doc['baseline'].get(name):continue
            fingerprint[name]=digest
            live=(self.root/name);before=live.read_bytes() if live.is_file() else None
            conflict=(hashlib.sha256(before).hexdigest() if before is not None else None)!=doc['baseline'].get(name)
            binary=Path(name).suffix.lower() in {'.png','.jpg','.webp','.wasm'}
            diff='Binary asset change' if binary else ''.join(difflib.unified_diff((before or b'').decode(errors='replace').splitlines(True),(raw or b'').decode(errors='replace').splitlines(True),fromfile='Live/'+name,tofile='Proposed/'+name))[:60000]
            rows.append({'path':name,'kind':'deleted' if raw is None else 'added' if before is None else 'modified','conflict':conflict,'diff':diff})
        revision=hashlib.sha256(json.dumps(fingerprint,sort_keys=True).encode()).hexdigest()
        result={'id':sid,'project':doc['project'],'changes':rows,'revision':revision}
        if include_files:result['files']={n:base64.b64encode(after[n]).decode() if n in after else None for n in fingerprint};result['baseline']=doc['baseline']
        return result
    def publish(self,sid,revision):
        with STUDIO_LOCK:
            review=self.review(sid,True)
            if review['revision']!=revision:raise ValueError('Changes changed after review. Review again before publishing.')
            if not review['changes']:raise ValueError('There are no changes to publish.')
            if any(x['conflict'] for x in review['changes']):raise ValueError('The live project changed. Start a fresh workspace to avoid overwriting it.')
            state=studio_bridge('/status')
            if state.get('sessions',{}).get(sid) in state.get('active',{}):raise ValueError('Wait for Codex to finish before publishing.')
            validated=studio_bridge('/validate',{'session':sid})
            if not validated.get('ok'):raise ValueError('Validation failed: '+validated.get('error','Check the files.'))
            # Read once more after checks and freeze the exact bytes for the supervisor.
            frozen=self.review(sid,True)
            if frozen['revision']!=revision:raise ValueError('Files changed during validation. Review again.')
            home=self.data.parent;request=home/'studio-request.json';status=home/'studio-status.json'
            if request.exists() or (status.exists() and json.loads(status.read_text()).get('state')=='pending'):raise ValueError('A publish is already running.')
            from history import atomic
            atomic(status,{'state':'pending','workspace':sid});atomic(request,frozen)
            return {'queued':True}
