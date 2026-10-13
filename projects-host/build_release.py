"""Curate and adapt the original projects; never package source secrets or toolchains."""
from pathlib import Path
import base64, hashlib, io, json, re, tarfile, time, py_compile, sys
from build_icons import icon_assets

BASE=Path(__file__).resolve().parent
NAS=BASE.parent
sys.path.insert(0,str(NAS/'layout-builder'))
from shared_build import shared, ha_bundle
WATCH=Path(r'C:/Users/lamon/Desktop/TacticalWatch')
LAMP=Path(r'C:/Users/lamon/Desktop/LampshadeStudio')
DOCS=Path(r'C:/Users/lamon/Documents/Codex')
OUT=BASE/'build'

def replace_once(text, old, new):
    if text.count(old)!=1: raise ValueError('Expected one adaptation point: '+old[:80])
    return text.replace(old,new,1)

def build():
    files={}
    def add(name,value): files[name]=value.encode() if isinstance(value,str) else value
    def read(p): return p.read_text(encoding='utf-8-sig')
    # Keep authentication outside tool version groups, and within the supervisor's
    # existing release allowlist. Restoring old editor code cannot undo permissions.
    server=read(BASE/'host/server.py').replace('from access import AccessControl, PROJECTS', read(BASE/'host/access.py'))
    server=server.replace('from seerr_access import SeerrAccess, SeerrAccessError',read(BASE/'host/seerr_access.py').replace('from access import OWNER_EMAIL, canonical_json, email_address, mailbox_key, display_email',''))
    server=server.replace('from studio import Studio, studio_bridge',read(BASE/'host/studio.py'))
    server=server.replace('from home_assistant import HomeAssistantSync, HAError, ha_validate',read(BASE/'host/home_assistant.py'))
    server=server.replace('from ha_automations import AutomationStudio, AutomationConflict',read(BASE/'host/ha_automations.py').replace('from home_assistant import HAError, HA_URL, ha_json, ha_atomic, ha_read',''))
    server=server.replace('from branding import branded_html, BRAND_ASSETS',read(BASE/'host/branding.py'))
    server=server.replace('from qr_backups import QRBackups, qr_backups_request','# BEGIN QR BACKUP MODULE\n'+read(BASE/'host/qr_backups.py')+'\n# END QR BACKUP MODULE')
    server=server.replace('from plant_tags import plant_tags_request','# BEGIN PLANT TAG MODULE\n'+read(BASE/'host/plant_tags.py').replace('from home_assistant import HA_URL','')+'\n# END PLANT TAG MODULE')
    server=server.replace('from image_review import ImageReview, image_review_request',read(BASE/'host/image_review.py'))
    server=server.replace('from building_notices import BuildingNotices, building_notices_request',read(BASE/'host/building_notices.py').replace('from home_assistant import HAError, HA_URL, ha_json, ha_atomic, ha_read','').replace('from ha_automations import automation_config_hash',''))
    server=server.replace('from karting import KartingStore, karting_request, karting_public_request','# BEGIN KARTING MODULE\n'+read(BASE/'host/karting.py').replace('from access import mailbox_key, OWNER_EMAIL','').replace('from karting_portal import KartingPortal, karting_portal_request, karting_public_request',read(BASE/'host/karting_portal.py'))+'\n# END KARTING MODULE')
    # Explicit Parts Bench code/assets only; library files, credentials, research,
    # source archives and runtime data never enter a release.
    from build_parts_bench_release import integrate as parts_integrate, assets as parts_assets, add_index as parts_index
    server=parts_integrate(server)
    add('server.py',server)
    files.update(parts_assets())
    # Explicit portal assets only. Private workbooks and NAS results are never seeds.
    for name in ('index.html','style.css','app.js','analysis.js','ratings.js','ratings-ui.js','drivers.js'):
        add('public/yas-karting/'+name,(BASE/'site/yas-karting'/name).read_bytes())
    add('history.py',read(BASE/'host/history.py'))
    add('public/index.html',read(BASE/'site/index.html').replace('__LIBRARY_STYLE__',read(BASE/'site/project-library.css')).replace('__LIBRARY_SCRIPT__',read(BASE/'site/project-library.js')))
    files['public/index.html']=parts_index(files['public/index.html'].decode()).encode()
    # Explicit owner-reviewer browser assets; no catalog, image, decision or personal path is packaged.
    for name in ('index.html','style.css','app.js'):
        add('public/image-review/'+name,(BASE/'image-review/public'/name).read_bytes())
    add('public/history.html',read(BASE/'site/history.html'))
    add('public/users.html',read(BASE/'site/users.html'))
    add('public/studio.html',read(BASE/'site/studio.html'))
    add('public/brand.png',(BASE/'site/brand.png').read_bytes())
    for name, data in icon_assets().items(): add('public/'+name, data)
    add('public/power-screw/index.html',read(BASE/'site/power-screw/index.html'))
    # Explicit QR assets and reviewed sender packages. Never publish fixtures, SDKs or private config.
    for name in ('index.html','style.css','app.js','sender.html','receiver.html','licenses.txt','backups.html','backups.js','sender-python.zip','sender-powershell.zip'):
        add('public/qr-transfer/'+name,(BASE/'site/qr-transfer'/name).read_bytes())
    # Self-contained Kua calculator: only these reviewed browser assets are published.
    for name in ('index.html','style.css','app.js','model.js','calendar.js'):
        add('public/kua-pairing/'+name,(BASE/'site/kua-pairing'/name).read_bytes())
    # Browser-only QR/CAD tool. Explicit assets only; never copy its build SDK or dependencies.
    for name in ('index.html','style.css','app.js','geometry.js','geometry.worker.js','import.js','export.js','batch.js','catalog.js','labels.js','vendor.js','manifold.wasm','pdf.js','pdf.worker.js','font.json','licenses.txt'):
        add('public/qr-studio/'+name,(BASE/'site/qr-studio'/name).read_bytes())
    converter=Path(r'C:/Users/lamon/Desktop/STL TO CAD CONVERTER/webapp/static')
    for p in converter.rglob('*'):
        if not p.is_file() or p.suffix not in {'.html','.css','.js'}: continue
        content=read(p).replace('/static/','/stl-converter/static/').replace('/api/','/stl-converter/api/')
        content=content.replace('Runs entirely on this machine','Conversion runs on your NAS · 32 MB maximum upload')
        if p.name=='app.js':
            content=content.replace('"use strict";','"use strict";\nconst safe=s=>String(s).replace(/[&<>"\\\']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",\'"\':"&quot;","\\\'":"&#39;"}[c]));')
            for old,new in [('${f.name}','${safe(f.name)}'),('${j.stage || "queued"}','${safe(j.stage || "queued")}'),('${j.message || "…"}','${safe(j.message || "…")}'),('${j.elapsed}','${safe(j.elapsed)}'),('${k}</i>','${safe(k)}</i>'),('${k}</td>','${safe(k)}</td>'),('${val}</td>','${safe(val)}</td>'),('s.innerHTML = msg +','s.innerHTML = (err ? safe(msg) : msg) +')]: content=content.replace(old,new)
            content=content.replace('always closes, 0 mm off the mesh','best for closed meshes; inspect the validation result')
        name='index.html' if p.name=='index.html' else 'static/'+p.relative_to(converter).as_posix()
        add('public/stl-converter/'+name,content)
    add('public/floor-plan-preview.png',(NAS/'ha-floor-plan/my-home-current.png').read_bytes())
    add('public/floor-plan-preview.svg',read(BASE/'site/floor-plan-preview.svg'))
    from build_floor_assets import floor_assets
    files.update(floor_assets())
    add('seeds/floor-plan.json',(NAS/'ha-floor-plan/my-home-current.json').read_bytes())
    # Studio 2 is rebuilt in memory using the project's own build recipe.
    designer=read(WATCH/'Watch-Face-Designer.html')
    studio=designer.replace('watchStudio-uploaded-20260914-v3','watchStudio-studio2-v1').replace('<title>Watch Face Studio</title>','<title>TacticalWatch · Studio 2</title>')
    seed=WATCH/'design/studio2-preview.json'
    if not seed.exists(): seed=WATCH/'design/approved-layout.json'
    baseline=json.dumps(json.loads(read(seed)),ensure_ascii=True).replace('<','\\u003c')
    studio=replace_once(studio,'let d=initial();','let d='+baseline+';')
    studio=studio.replace('</html>','<style>'+read(WATCH/'studio/simple.css')+'</style>'+''.join('<script>'+read(WATCH/'studio'/n)+'</script>' for n in ['event-model.js','interaction-model.js','interaction-view.js','simple.js'])+'</html>')
    transport="""<script>
const nasOriginalFetch=window.fetch.bind(window);let nasDraftEtag=null,nasDraftConflict=false;
window.fetch=async function(input,options){const isDraft=typeof input==='string'&&input==='/garmin/api/draft';let o={...options};if(isDraft&&o.method==='PUT'){if(nasDraftConflict||!nasDraftEtag)return new Response('',{status:409});o.headers={...o.headers,'If-Match':nasDraftEtag};}const r=await nasOriginalFetch(input,o);if(isDraft){if(r.ok)nasDraftEtag=r.headers.get('ETag');if(r.status===409){nasDraftConflict=true;alert('A newer watch draft exists on the NAS. Save your design as JSON, then reload to continue.');}}return r;};
</script>"""
    for name,html in [('index.html',studio),('Watch-Face-Studio.html',studio),('Watch-Face-Designer.html',designer)]:
        html=html.replace("location.protocol==='http:'","['http:','https:'].includes(location.protocol)")
        html=html.replace("'/api/","'/garmin/api/").replace('"/api/','"/garmin/api/')
        html=html.replace("'/Watch-Face-","'/garmin/Watch-Face-").replace('"/Watch-Face-','"/garmin/Watch-Face-')
        html=re.sub(r'(<html\b[^>]*>)',lambda m:m.group(1)+transport,html,count=1,flags=re.I)
        html=html.replace('Live with Codex · changes from this conversation appear here','Saved on NAS · shared watch draft')
        add('public/garmin/'+name,html)
    draft=WATCH/'.local-backup/studio-live/draft.json'
    add('seeds/garmin.json',(draft if draft.exists() else WATCH/'design/approved-layout.json').read_bytes())
    for name in ['server.py','geometry.py','bulb.py','exports.py','nx_export.py','nx_translate.py','chandelier.py','chandelier_web.py','chandelier_assembly.py','chandelier_bridge.py']:
        add('lampshade/'+('server_lamp.py' if name=='server.py' else name),read(LAMP/name))
    for p in (LAMP/'public').rglob('*'):
        if not p.is_file(): continue
        content=p.read_bytes()
        if p.suffix in {'.js','.html','.css'}:
            content=read(p).replace("'/api/","'/lampshade/api/").replace('`/api/','`/lampshade/api/').replace('"/api/','"/lampshade/api/')
        if p.name=='index.html': content=content.replace('LOCAL WORKSPACE','NAS WORKSPACE').replace('<button id="load"','<button id="nasSave" class="quiet">Save to NAS</button><button id="load"')
        if p.name=='app.js':
            content=replace_once(content,"state.params={...config.presets.spiral};","state.params={...config.presets.spiral};await loadNasDesign();")
            content="import {createDesignStore} from './chandelier-store.js';\nconst nasLampStore=createDesignStore(fetch.bind(window),'/api/designs/lampshade');\n"+content
            content+="""
async function loadNasDesign(){try{const saved=await nasLampStore.load();if(saved?.parameters){state.params={...state.presets[saved.parameters.style||'spiral'],...saved.parameters};state.exportFormat=['stl','step','brep'].includes(saved.exportOptions?.exportFormat)?saved.exportOptions.exportFormat:'stl';state.stlQuality=saved.exportOptions?.stlQuality||'balanced';}}catch(e){setStatus(e.message,true);}}
$('nasSave').addEventListener('click',async()=>{if(!state.valid)return;try{await nasLampStore.save({parameters:state.params,exportOptions:{exportFormat:state.exportFormat,stlQuality:state.stlQuality}});setStatus('Design saved on NAS');}catch(e){setStatus(e.message,true);}});
"""
        add('public/lampshade/'+p.relative_to(LAMP/'public').as_posix(),content)
    lampseed=LAMP/'backups/before-export-options/your-design.json'
    add('seeds/lampshade.json',lampseed.read_bytes())
    extras={
      'mechanisms':DOCS/'2026-09-06/im/outputs/mechanism-workbench.html',
      'nx-shortcuts':DOCS/'2026-09-06/can/outputs/nx-shortcut-workbench/NX-Shortcut-Workbench.html',
      'print-farm':DOCS/'2026-09-05/farm-request-management-tool/outputs/3D_Print_Farm_Tracker.html'}
    for slug,p in extras.items(): add('public/'+slug+'/index.html',p.read_bytes())
    # A compact home link in each editor, keeping the original editing surface intact.
    for n in list(files):
        if n.startswith('public/') and n.endswith('.html') and n!='public/index.html' and not n.startswith('public/qr-transfer/'):
            html=files[n].decode('utf-8-sig')
            link='<a href="/" title="Back to all projects" style="position:fixed;bottom:10px;left:10px;z-index:99999;padding:6px 11px;border:1px solid #607064;border-radius:20px;background:#14221fea;color:#e4efdc;font:12px system-ui;text-decoration:none">← Projects</a>'
            html=html.replace('</body>',link+'</body>',1)
            files[n]=html.encode()
    for n,b in files.items():
        if n.endswith('.py'): compile(b.decode(),n,'exec')
    manifest={'id':time.strftime('%Y-%m-%d %H:%M:%S UTC',time.gmtime()),'files':{n:hashlib.sha256(b).hexdigest() for n,b in sorted(files.items())}}
    add('release.json',json.dumps(manifest,indent=2))
    OUT.mkdir(exist_ok=True)
    archive=io.BytesIO()
    with tarfile.open(fileobj=archive,mode='w:gz') as tar:
        for n,b in sorted(files.items()):
            info=tarfile.TarInfo(n);info.size=len(b);info.mode=0o644;tar.addfile(info,io.BytesIO(b))
    path=OUT/'release.tar.gz';path.write_bytes(archive.getvalue())
    (OUT/'manifest.json').write_text(json.dumps(manifest,indent=2))
    print('Built',manifest['id'],len(files),'files;',path.stat().st_size,'bytes')
    return path,manifest

if __name__=='__main__': build()
