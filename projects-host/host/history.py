"""Five distinct published versions per tool; design data is never rolled back."""
import base64,hashlib,json,os,shutil,time,uuid
from pathlib import Path
HOME=Path('/srv')
GROUPS={
 'yas-karting':['public/yas-karting/'],
 'qr-transfer':['public/qr-transfer/'],
 'floor-plan':['public/floor-plan/'],
 'garmin':['public/garmin/'],
 'lampshade':['public/lampshade/','lampshade/'],
 'mechanisms':['public/mechanisms/'],
 'nx-shortcuts':['public/nx-shortcuts/'],
 'print-farm':['public/print-farm/'],
 'power-screw':['public/power-screw/'], 'stl-converter':['public/stl-converter/'],
 'qr-studio':['public/qr-studio/'], 'kua-pairing':['public/kua-pairing/']}

def atomic(path,value):
    temp=path.with_suffix('.tmp');temp.write_text(json.dumps(value,indent=2));os.replace(temp,path)
def catalogue():
    file=HOME/'tool-history.json'
    return json.loads(file.read_text()) if file.exists() else {}
def tool_files(root,slug):
    return sorted(p.relative_to(root).as_posix() for prefix in GROUPS[slug] for p in (root/prefix).rglob('*') if p.is_file() and '__pycache__' not in p.parts)
def version(root,slug):
    files=tool_files(root,slug)
    digest=hashlib.sha256()
    for name in files:digest.update(name.encode()+b'\0'+hashlib.sha256((root/name).read_bytes()).digest())
    return digest.hexdigest()[:16],files
def record(root):
    history=catalogue();manifest=json.loads((root/'release.json').read_text())
    for slug in GROUPS:
        ver,files=version(root,slug)
        if not files:continue
        previous=history.get(slug,[])
        if previous and previous[0]['version']==ver:continue
        row={'version':ver,'published':manifest['id'],'release':root.name,'files':files}
        history[slug]=([row]+[r for r in previous if r['version']!=ver])[:5]
    atomic(HOME/'tool-history.json',history)
def prepare_restore(slug,ver):
    if slug not in GROUPS:raise ValueError('Unknown tool')
    row=next((r for r in catalogue().get(slug,[]) if r['version']==ver),None)
    if not row:raise ValueError('Version no longer retained')
    current=(HOME/'current').resolve()
    if version(current,slug)[0]==ver:raise ValueError('This version is already active')
    source=HOME/'releases'/row['release']
    if version(source,slug)[0]!=ver:raise ValueError('Historical files failed verification')
    target=HOME/'releases'/('restore-'+uuid.uuid4().hex[:16])
    shutil.copytree(current,target,ignore=shutil.ignore_patterns('__pycache__'))
    for prefix in GROUPS[slug]:
        dest=(target/prefix).resolve()
        # This deletes only generated code in the newly created release copy.
        if not dest.is_relative_to(target.resolve()):raise ValueError('Invalid restore path')
        if dest.exists():shutil.rmtree(dest)
        shutil.copytree(source/prefix,dest)
    files={p.relative_to(target).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in target.rglob('*') if p.is_file() and p.name!='release.json'}
    manifest={'id':time.strftime('%Y-%m-%d %H:%M:%S UTC',time.gmtime()),'restored':{'tool':slug,'version':ver},'files':files}
    atomic(target/'release.json',manifest)
    return target,manifest

def rehash(target,extra=None):
    files={p.relative_to(target).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in target.rglob('*') if p.is_file() and p.name!='release.json' and '__pycache__' not in p.parts}
    manifest=json.loads((target/'release.json').read_text());manifest['files']=files;manifest.update(extra or {});atomic(target/'release.json',manifest)
    return manifest

def overlays():
    path=HOME/'data/studio-overrides.json'
    return json.loads(path.read_text()) if path.exists() else {}

def restore_committed(slug,target):
    """Keep a restored tool authoritative when a later PC release is uploaded."""
    values=overlays()
    for name,row in values.items():
        if not any(name.startswith(prefix) for prefix in GROUPS[slug]):continue
        path=target/name;raw=path.read_bytes() if path.is_file() else None
        row['liveHash']=hashlib.sha256(raw).hexdigest() if raw is not None else None
        row['content']=base64.b64encode(raw).decode() if raw is not None else None
    atomic(HOME/'data/studio-overrides.json',values)

def apply_overrides(target):
    for name,row in overlays().items():
        dest=target/name;incoming=hashlib.sha256(dest.read_bytes()).hexdigest() if dest.is_file() else None
        if incoming==row['liveHash']:continue
        if incoming!=row['sourceHash']:raise ValueError('Remote edits conflict with local source: '+name+'. Reconcile in Project Studio before publishing.')
        if row['content'] is None:dest.unlink(missing_ok=True)
        else:dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(base64.b64decode(row['content'],validate=True))
    return rehash(target)

def prepare_studio(job):
    slug=job['project'];changes=job['files'];baseline=job['baseline'];current=(HOME/'current').resolve()
    if slug not in GROUPS or not changes or len(changes)>200:raise ValueError('Invalid project changes.')
    total=0;decoded={}
    for name,value in changes.items():
        dest=(current/name).resolve()
        if not dest.is_relative_to(current) or '\\' in name or '..' in Path(name).parts or not any(name.startswith(p) for p in GROUPS[slug]):raise ValueError('Invalid change path.')
        live=hashlib.sha256(dest.read_bytes()).hexdigest() if dest.is_file() else None
        if live!=baseline.get(name):raise ValueError('Project changed since this workspace was created. Review a fresh workspace.')
        data=base64.b64decode(value,validate=True) if value is not None else None
        total+=len(data or b'')
        if total>40000000:raise ValueError('Changes too large.')
        if data is not None and name.endswith('.py'):compile(data,name,'exec')
        decoded[name]=data
    target=HOME/'releases'/('studio-'+uuid.uuid4().hex[:16]);shutil.copytree(current,target,ignore=shutil.ignore_patterns('__pycache__'))
    for name,data in decoded.items():
        dest=target/name
        if data is None:dest.unlink(missing_ok=True)
        else:dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(data)
    return target,rehash(target,{'id':time.strftime('%Y-%m-%d %H:%M:%S UTC',time.gmtime()),'studio':{'workspace':job['id'],'project':slug}})

def studio_committed(job):
    values=overlays()
    for name,content in job['files'].items():
        source=values.get(name,{}).get('sourceHash',job['baseline'].get(name))
        live=hashlib.sha256(base64.b64decode(content)).hexdigest() if content is not None else None
        values[name]={'sourceHash':source,'liveHash':live,'content':content}
    atomic(HOME/'data/studio-overrides.json',values)
    meta=HOME/'data/private/studio'/(job['id']+'.json');doc=json.loads(meta.read_text())
    for name,content in job['files'].items():
        if content is None:doc['baseline'].pop(name,None)
        else:doc['baseline'][name]=hashlib.sha256(base64.b64decode(content)).hexdigest()
    atomic(meta,doc)
