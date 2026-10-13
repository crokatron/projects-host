"""Owner-only NAS -> HA integration. Credentials and retry state never enter releases."""
import base64, copy, hashlib, html, json, math, os, re, secrets, select, socket, ssl, struct, threading, time
import urllib.parse, urllib.request
from pathlib import Path

HA_URL = 'http://192.168.0.223'
HA_CLIENT = 'https://projects.mungoon.net/'
HA_REDIRECT = HA_CLIENT + 'floor-plan/'
HA_DASHBOARD = 'floor-plan'
HA_CARD_RESOURCE = '/local/mungoon/ha-floor-card-v2.js?v=20261011-hub-compact-1'
HA_MOVE_EVENT = 'mungoon_floor_move_request'
HA_MOVE_RESULT = 'mungoon_floor_move_result'

class HAError(Exception):
    pass

def ha_json(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()

def ha_atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'wb') as out: out.write(ha_json(value))
    os.replace(temp, path)
    os.chmod(path, 0o600)

def ha_read(path, default):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else copy.deepcopy(default)

class HAWebSocket:
    """Small bounded RFC6455 client, avoiding a new NAS runtime dependency."""
    def __init__(self, token, url=HA_URL):
        self.sock = None
        self.id = 0
        try:
            parsed = urllib.parse.urlparse(url)
            self.sock = socket.create_connection((parsed.hostname, parsed.port or (443 if parsed.scheme == 'https' else 80)), timeout=15)
            if parsed.scheme == 'https': self.sock = ssl.create_default_context().wrap_socket(self.sock, server_hostname=parsed.hostname)
            key = base64.b64encode(os.urandom(16)).decode()
            self.sock.sendall((f'GET /api/websocket HTTP/1.1\r\nHost: {parsed.netloc}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n').encode())
            header = b''
            while not header.endswith(b'\r\n\r\n'):
                header += self.exact(1)
                if len(header) > 16384: raise HAError('Invalid HA connection response.')
            lines = header.decode().split('\r\n')
            fields = {k.lower().strip():v.strip() for k,v in (line.split(':',1) for line in lines[1:] if ':' in line)}
            accept = base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
            if ' 101 ' not in lines[0] or fields.get('sec-websocket-accept') != accept: raise HAError('HA WebSocket upgrade failed.')
            if self.receive().get('type') != 'auth_required': raise HAError('Unexpected HA authentication response.')
            self.send({'type':'auth', 'access_token':token})
            if self.receive().get('type') != 'auth_ok': raise HAError('Home Assistant sign-in expired. Reconnect HA.')
        except Exception:
            self.close()
            raise

    def exact(self, count):
        chunks = bytearray()
        while len(chunks) < count:
            part = self.sock.recv(count-len(chunks))
            if not part: raise HAError('Home Assistant closed the connection.')
            chunks.extend(part)
        return bytes(chunks)

    def frame(self, data, opcode=1):
        size = len(data)
        head = bytes([0x80 | opcode, 0x80 | (size if size < 126 else 126 if size < 65536 else 127)])
        if size >= 126: head += struct.pack('!H' if size < 65536 else '!Q', size)
        mask = os.urandom(4)
        self.sock.sendall(head + mask + bytes(b ^ mask[i%4] for i,b in enumerate(data)))

    def send(self, value): self.frame(ha_json(value))

    def receive(self):
        data = bytearray()
        while True:
            first, second = self.exact(2)
            opcode = first & 15
            size = second & 127
            if first & 0x70 or second & 128: raise HAError('Unsupported HA WebSocket frame.')
            if size == 126: size = struct.unpack('!H', self.exact(2))[0]
            elif size == 127: size = struct.unpack('!Q', self.exact(8))[0]
            if size + len(data) > 16000000: raise HAError('HA response is too large.')
            payload = self.exact(size)
            if opcode == 8: raise HAError('Home Assistant closed the connection.')
            if opcode == 9: self.frame(payload, 10); continue
            if opcode == 10: return {'type':'pong'}
            if opcode not in (0,1): raise HAError('Unsupported HA message.')
            data.extend(payload)
            if first & 128: return json.loads(data)

    def call(self, command, **values):
        self.id += 1
        self.send({'id':self.id, 'type':command, **values})
        for _ in range(100):
            message = self.receive()
            if message.get('id') != self.id: continue
            if not message.get('success'):
                # Do not forward remote error bodies (which can contain private data).
                raise HAError('Home Assistant rejected '+command+'. Check administrator access and retry.')
            return message.get('result')
        raise HAError('Home Assistant did not confirm the request.')

    def close(self):
        if self.sock: self.sock.close(); self.sock = None
    def __enter__(self): return self
    def __exit__(self, *args): self.close()

def ha_number(n): return isinstance(n, (int,float)) and not isinstance(n,bool) and math.isfinite(n) and abs(n) < 100000

def ha_inside(x, y, points):
    inside = False
    for a,b in zip(points, points[-1:]+points[:-1]):
        if (a[1]>y) != (b[1]>y) and x < (b[0]-a[0])*(y-a[1])/(b[1]-a[1])+a[0]: inside = not inside
    return inside

def ha_room(plan, device):
    return next((r for r in plan['rooms'] if ha_inside(device['x'], device['y'], r['points'])), None)

def ha_revision(plan):
    return '"'+hashlib.sha256(ha_json(plan)).hexdigest()+'"'

def ha_move_device(plan, request):
    """Move one existing marker; never accept a replacement layout from HA."""
    fields={'request_id','revision','device_id','room_id','x','y'}
    if not isinstance(request,dict) or set(request)!=fields:raise HAError('Invalid device move.')
    if not isinstance(request['request_id'],str) or not re.fullmatch(r'[a-zA-Z0-9-]{16,80}',request['request_id']):raise HAError('Invalid move reference.')
    if request['revision']!=ha_revision(plan):raise HAError('The layout changed elsewhere. Refresh the floor plan before moving this device.')
    if not isinstance(request['device_id'],str) or not isinstance(request['room_id'],str) or not all(ha_number(request[k]) for k in ('x','y')):raise HAError('Invalid device position.')
    result=copy.deepcopy(plan)
    marker=next((d for d in result.get('devices',[]) if d['device_id']==request['device_id']),None)
    if not marker:raise HAError('This device is no longer placed. Refresh the floor plan.')
    room=next((r for r in result['rooms'] if r['id']==request['room_id']),None)
    x,y=round(request['x'],2),round(request['y'],2)
    actual=ha_room(result,{'x':x,'y':y})
    if request['room_id'] and (not room or not actual or actual['id']!=room['id']):raise HAError('Place the pin inside the chosen room.')
    marker.update(x=x,y=y)
    # Keep the established Room · Device naming style; leave custom names alone.
    name=result.get('ha_device_names',{}).get(marker['device_id']) or marker.get('ha_name') or marker.get('name','')
    prefixes=[(r.get('ha_name') or r['name'])+' · ' for r in result['rooms']]
    prefix=next((p for p in prefixes if name.startswith(p)),None)
    if actual and prefix:
        name=(actual.get('ha_name') or actual['name'])+' · '+name[len(prefix):]
        marker.update(name=name,ha_name=name)
        result.setdefault('ha_device_names',{})[marker['device_id']]=name
    ha_validate(result)
    return result,marker,actual

def ha_validate(plan):
    if not isinstance(plan,dict) or not isinstance(plan.get('rooms'),list) or not isinstance(plan.get('items'),list): raise ValueError('Invalid floor plan.')
    if len(plan['rooms']) > 100 or len(plan['items']) > 1000: raise ValueError('Floor plan is too large.')
    def short(v): return isinstance(v,str) and 0 < len(v) <= 150
    def point(p): return isinstance(p,list) and len(p)==2 and all(ha_number(n) for n in p)
    if 'north_bearing' in plan and (not ha_number(plan['north_bearing']) or not 0<=plan['north_bearing']<360): raise ValueError('Invalid drawing north bearing.')
    if 'north_reference' in plan and (not isinstance(plan['north_reference'],str) or len(plan['north_reference'])>200): raise ValueError('Invalid north reference.')
    ids = set()
    for r in plan['rooms']:
        if not isinstance(r,dict) or not short(r.get('id')) or r['id'] in ids or not short(r.get('name')) or not isinstance(r.get('points'),list) or not 3<=len(r['points'])<=100 or not all(point(p) for p in r['points']) or not point(r.get('label')): raise ValueError('Invalid room geometry.')
        if r.get('ha_name') is not None and not short(r['ha_name']): raise ValueError('Invalid HA room name.')
        if 'room_tile' in r:
            t=r['room_tile'];metrics={'temperature','humidity','air_quality','pm25','carbon_dioxide','aqi','pm10','volatile_organic_compounds','volatile_organic_compounds_parts','illuminance'}
            if not isinstance(t,dict) or set(t)-{'title','visible','width','color','metrics'}:raise ValueError('Invalid room tile.')
            if 'title' in t and (not isinstance(t['title'],str) or len(t['title'])>90):raise ValueError('Invalid tile title.')
            if 'visible' in t and not isinstance(t['visible'],bool):raise ValueError('Invalid tile visibility.')
            if 'width' in t and (not ha_number(t['width']) or not 80<=t['width']<=400):raise ValueError('Tile width must be 80–400 cm.')
            if 'color' in t and (not isinstance(t['color'],str) or not re.fullmatch(r'#[a-fA-F0-9]{6}',t['color'])):raise ValueError('Invalid tile colour.')
            if 'metrics' in t and (not isinstance(t['metrics'],list) or not 1<=len(t['metrics'])<=4 or any(not isinstance(m,str) or m not in metrics for m in t['metrics']) or len(set(t['metrics']))!=len(t['metrics'])):raise ValueError('Choose 1–4 tile readings.')
        ids.add(r['id'])
    for item in plan['items']:
        if not isinstance(item,dict) or not all(ha_number(item.get(k)) for k in ('x','y','w','h','rotation')) or item['w']<5 or item['h']<5: raise ValueError('Invalid furniture geometry.')
        if any(k in item and not isinstance(item[k],bool) for k in ('mirrorX','mirrorY')):raise ValueError('Invalid furniture mirror setting.')
    walls=plan.get('walls',[])
    if not isinstance(walls,list) or len(walls)>500: raise ValueError('Invalid internal walls.')
    for wall in walls:
        if not isinstance(wall,dict) or not short(wall.get('id')) or wall['id'] in ids or not short(wall.get('name')) or not point(wall.get('a')) or not point(wall.get('b')) or math.dist(wall['a'],wall['b'])<1 or not ha_number(wall.get('thickness')) or not 1<=wall['thickness']<=100: raise ValueError('Invalid internal wall.')
        ids.add(wall['id'])
    for room in plan['rooms']:
        if 'labelManual' in room and not isinstance(room['labelManual'],bool): raise ValueError('Invalid room label.')
    dimensions=plan.get('dimensions',[])
    if not isinstance(dimensions,list) or len(dimensions)>500: raise ValueError('Invalid dimensions.')
    def endpoint(anchor):
        if not isinstance(anchor,dict): return None
        if 'wallId' in anchor:
            wall=next((w for w in walls if w['id']==anchor['wallId']),None)
            return wall.get(anchor.get('end')) if wall and anchor.get('end') in ('a','b') else None
        room=next((r for r in plan['rooms'] if r['id']==anchor.get('roomId')),None)
        nodes=room.get('nodeIds',[]) if room else []
        return room['points'][nodes.index(anchor['nodeId'])] if anchor.get('nodeId') in nodes and nodes.index(anchor['nodeId'])<len(room['points']) else None
    for dimension in dimensions:
        if not isinstance(dimension,dict): raise ValueError('Invalid dimension.')
        if 'driving' not in dimension: continue
        constraint=dimension['driving'];a=endpoint(dimension.get('aAnchor'));b=endpoint(dimension.get('bAnchor'))
        if not isinstance(constraint,dict) or not ha_number(constraint.get('length')) or not 1<=constraint['length']<=50000 or not point(constraint.get('direction')) or abs(math.hypot(*constraint['direction'])-1)>1e-6 or a is None or b is None: raise ValueError('Invalid driving dimension.')
        if any(abs(b[i]-a[i]-constraint['direction'][i]*constraint['length'])>.001 for i in (0,1)): raise ValueError('Overdefined: geometry does not match its driving dimensions.')
    device_names=plan.get('ha_device_names',{})
    if not isinstance(device_names,dict) or len(device_names)>1000 or any(not short(k) or not short(v) for k,v in device_names.items()):raise ValueError('Invalid HA device names.')
    devices = plan.get('devices', [])
    if not isinstance(devices,list) or len(devices)>1000: raise ValueError('Invalid HA placements.')
    seen = set()
    for d in devices:
        if not isinstance(d,dict) or not short(d.get('device_id')) or d['device_id'] in seen or not short(d.get('entity_id')) or not all(ha_number(d.get(k)) for k in ('x','y')): raise ValueError('Invalid HA device placement.')
        if not isinstance(d.get('name',''),str) or len(d.get('name',''))>150: raise ValueError('Invalid device name.')
        if 'ha_name' in d and (not isinstance(d['ha_name'],str) or not d['ha_name'].strip() or len(d['ha_name'])>150): raise ValueError('Invalid HA device name.')
        if 'icon' in d and (not isinstance(d['icon'],str) or len(d['icon'])>100 or not re.fullmatch(r'mdi:[a-z0-9]+(?:-[a-z0-9]+)*',d['icon'])): raise ValueError('Invalid device icon.')
        seen.add(d['device_id'])
    controls=plan.get('ha_controls',[])
    if not isinstance(controls,list) or len(controls)>100:raise ValueError('Invalid HA control shortcuts.')
    seen=set()
    for c in controls:
        if not isinstance(c,dict) or not short(c.get('id')) or c['id'] in seen or not short(c.get('name')) or not short(c.get('entity_id')) or not all(ha_number(c.get(k)) for k in ('x','y')):raise ValueError('Invalid HA control shortcut.')
        if not isinstance(c.get('navigation_path'),str) or not re.fullmatch(r'/[a-zA-Z0-9_-]+/[a-zA-Z0-9_-]+',c['navigation_path']):raise ValueError('Control shortcuts must open a local HA dashboard view.')
        if 'icon' in c and (not isinstance(c['icon'],str) or len(c['icon'])>100 or not re.fullmatch(r'mdi:[a-z0-9]+(?:-[a-z0-9]+)*',c['icon'])):raise ValueError('Invalid control shortcut icon.')
        seen.add(c['id'])

def ha_area_names(plan):
    names = {}
    for r in plan['rooms']:
        name = r.get('ha_name') or r['name']
        if not r.get('ha_name') and sum(x['name'].casefold()==name.casefold() for x in plan['rooms'])>1: name += ' ('+r['id']+')'
        if name.casefold() in [v.casefold() for v in names.values()]: raise HAError('HA room names must be unique. Set a distinct Home Assistant room name in the room inspector.')
        names[r['id']] = name
    return names

def ha_dark(color):
    try:
        if len(color)!=7 or color[0]!='#': raise ValueError()
        channels = [int(color[i:i+2],16) for i in (1,3,5)]
        return '#'+''.join(f'{round(c*.20+b*.80):02x}' for c,b in zip(channels,(18,30,34)))
    except (ValueError,TypeError): return '#26383c'

def ha_floor_svg(plan):
    """Render the saved geometry, never a screenshot or stale seed."""
    points = [p for r in plan['rooms'] for p in r['points']]
    points += [r['label'] for r in plan['rooms']]
    points += [p for wall in plan.get('walls',[]) for p in (wall['a'],wall['b'])]
    for i in plan['items']:
        radius=math.hypot(i['w'],i['h'])*(1.6 if i.get('kind')=='door' else .5)
        points += [[i['x']-radius,i['y']-radius],[i['x']+radius,i['y']+radius]]
    for d in plan.get('devices',[]): points += [[d['x']-30,d['y']-30],[d['x']+30,d['y']+30]]
    if not points: points = [[0,0],[1000,1000]]
    x,y = min(p[0] for p in points)-65,min(p[1] for p in points)-65
    w,h = max(p[0] for p in points)-x+65,max(p[1] for p in points)-y+65
    out=[f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{x} {y} {w} {h}" width="1600" height="{1600*h/w}"><rect x="{x}" y="{y}" width="{w}" height="{h}" fill="#101b20"/>']
    for r in plan['rooms']:
        polygon=' '.join(f'{a},{b}' for a,b in r['points'])
        out.append(f'<polygon points="{polygon}" fill="{ha_dark(r.get("color"))}" stroke="#728d92" stroke-width="5" stroke-linejoin="round"/>')
    for wall in plan.get('walls',[]):
        a,b=wall['a'],wall['b']
        out.append(f'<line x1="{a[0]}" y1="{a[1]}" x2="{b[0]}" y2="{b[1]}" stroke="#9ab0b5" stroke-width="{wall["thickness"]}" stroke-linecap="butt"/>')
    for i in plan['items']:
        iw,ih=i['w'],i['h'];kind=i.get('kind');ix,iy=-iw/2,-ih/2
        color=html.escape(i.get('color','#718088'),quote=True)
        if not color.startswith('#') or len(color)!=7: color='#718088'
        out.append(f'<g transform="translate({i["x"]} {i["y"]}) rotate({i["rotation"]})" fill="{ha_dark(color)}" stroke="#819599" stroke-width="2">')
        if kind=='door':
            direction=(1 if i.get('swing', 'ccw' if i.get('flip') else 'cw')=='cw' else -1)*(-1 if i.get('flip') else 1)
            out.append(f'<g transform="translate(0 {iy}) scale({-1 if i.get("flip") else 1} {direction}) translate(0 {-iy})"><path d="M{ix} {iy}h{iw}" stroke="#101b20" stroke-width="15"/><path d="M{ix} {iy}v{ih}" stroke="#ec8084" stroke-width="4"/><path d="M{ix} {ih/2}A{iw} {ih} 0 0 0 {iw/2} {iy}" fill="none" stroke="#ec8084" stroke-dasharray="5 3"/></g>')
        elif kind=='sliding-door': out.append(f'<path d="M{ix} 0h{iw}" stroke="#101b20" stroke-width="20"/><rect x="{ix}" y="{iy}" width="{iw*.57}" height="{ih*.42}" stroke="#ec8084"/><rect x="{ix+iw*.43}" y="{ih*.08}" width="{iw*.57}" height="{ih*.42}" stroke="#ec8084"/>')
        elif kind in ('plant','round-table'): out.append(f'<ellipse rx="{iw/2}" ry="{ih/2}"/>')
        else:
            out.append(f'<rect x="{ix}" y="{iy}" width="{iw}" height="{ih}" rx="5"/>')
            if kind in ('bed','sofa','armchair','toilet','tv','wardrobe','desk','shelf','fridge'): out.append(f'<path d="M{ix+5} {iy+ih*.28}h{iw-10}" fill="none"/>')
        out.append('</g>')
    for r in plan['rooms']:
        lx,ly=r['label'];words=r['name'].split();lines=[];line=''
        for word in words:
            if line and len(line+' '+word)>19: lines.append(line);line=word
            else: line+=(' ' if line else '')+word
        if line: lines.append(line)
        out.append(f'<text x="{lx}" y="{ly-(len(lines)-1)*11}" fill="#e0eceb" text-anchor="middle" font-family="Segoe UI,Arial,sans-serif" font-size="19" font-weight="600">')
        out.extend(f'<tspan x="{lx}" dy="{22 if n else 0}">{html.escape(line)}</tspan>' for n,line in enumerate(lines))
        out.append('</text>')
    out.append('</svg>')
    return ''.join(out), (x,y,w,h)

def ha_device_path(device_id):
    return 'device-'+hashlib.sha256(device_id.encode()).hexdigest()[:24]


def ha_camera_controls(entity_id, info):
    """HA-session-only controls from the camera's reported ONVIF capabilities."""
    if not info.get('relative') and not info.get('continuous'):return []
    mode='RelativeMove' if info.get('relative') else 'ContinuousMove'
    def button(name,icon,data):
        return {'type':'button','name':name,'icon':icon,'show_state':False,
            'tap_action':{'action':'perform-action','perform_action':'onvif.ptz',
                'target':{'entity_id':entity_id},'data':data},
            'hold_action':{'action':'none'}}
    directions=[]
    for name,icon,axis,direction in (
        ('Up','mdi:arrow-up','tilt','UP'),('Left','mdi:arrow-left','pan','LEFT'),
        ('Stop','mdi:stop',None,None),('Right','mdi:arrow-right','pan','RIGHT'),
        ('Down','mdi:arrow-down','tilt','DOWN')):
        # These cameras report horizontal movement opposite to their
        # installed view. Keep the correction tied to these exact cameras;
        # tilt, Stop, presets and every other camera retain their mapping.
        if entity_id in {'camera.tapo_c210_38e9_mainstream','camera.tapo_c210_cf77_mainstream'} and axis=='pan':
            direction={'LEFT':'RIGHT','RIGHT':'LEFT'}[direction]
        data={'move_mode':'Stop'} if axis is None else {'move_mode':mode,axis:direction,'speed':0.25}
        if axis is not None:
            data.update({'distance':0.05} if mode=='RelativeMove' else {'continuous_duration':0.25})
        directions.append(button(name,icon,data))
    cards=[{'type':'grid','columns':5,'square':False,'cards':directions}]
    presets=[button('Preset '+token,'mdi:camera-marker',{'move_mode':'GotoPreset','preset':token})
        for token in info.get('presets',[])]
    if presets:cards.append({'type':'grid','columns':min(3,len(presets)),'square':False,'cards':presets})
    return cards


# BEGIN ANDROID TV CONTROLS
def ha_tv_remote(entity, name):
    """Native cards: HA app navigation and service calls stay inside HA."""
    def key(label, icon, command=None, service='send_command'):
        action={'action':'perform-action','perform_action':'remote.'+service,'target':{'entity_id':entity}}
        if command:action['data']={'command':command}
        return {'type':'button','name':label,'icon':'mdi:'+icon,'show_state':False,
                'tap_action':action,'hold_action':{'action':'none'}}
    def grid(keys):return {'type':'grid','columns':3,'square':False,'cards':[key(*k) for k in keys]}
    return {'type':'vertical-stack','cards':[
        {'type':'tile','entity':entity,'name':name,'icon':'mdi:remote-tv','tap_action':{'action':'more-info'},'icon_tap_action':{'action':'more-info'}},
        grid([('Back','arrow-left','BACK'),('Up','chevron-up','DPAD_UP'),('Home','home','HOME'),
              ('Left','chevron-left','DPAD_LEFT'),('OK','checkbox-blank-circle-outline','DPAD_CENTER'),('Right','chevron-right','DPAD_RIGHT'),
              ('Menu','menu','MENU'),('Down','chevron-down','DPAD_DOWN'),('Play / Pause','play-pause','MEDIA_PLAY_PAUSE')]),
        grid([('Volume −','volume-minus','VOLUME_DOWN'),('Mute','volume-mute','VOLUME_MUTE'),('Volume +','volume-plus','VOLUME_UP')]),
        grid([('Previous','skip-previous','MEDIA_PREVIOUS'),('Stop','stop','MEDIA_STOP'),('Next','skip-next','MEDIA_NEXT')]),
        {'type':'horizontal-stack','cards':[key('Turn on','power',service='turn_on'),key('Turn off','power-off',service='turn_off')]}
    ]}

def ha_tv_view(dashboard, entities, devices):
    """Retire only our old nested remote tab; retain any user-added cards."""
    views=dashboard.setdefault('views',[])
    for view in list(views):
        if view.get('mungoon_tv_remotes') is not True:continue
        cards=view.get('cards',[])
        owned=[c for c in cards if c.get('mungoon_tv_remote_controls') is True]
        if len(owned)!=1:raise HAError('The old remote page could not be identified safely.')
        if len(cards)==1:views.remove(view)
        else:
            view['cards'][cards.index(owned[0])]={'type':'button','name':'Open Remotes','icon':'mdi:remote-tv',
                'mungoon_tv_remote_controls':True,'tap_action':{'action':'navigate','navigation_path':'/media-remotes/home'}}

HA_REMOTES_DASHBOARD='media-remotes'
HA_REMOTES_RESOURCE='/local/mungoon/ha-remotes-card-20261011.js?v=6-intro'

def ha_soundbar_controls(entities, device_id):
    return [{'entity':eid,'name':e.get('name') or e.get('original_name') or eid}
        for eid,e in sorted(entities.items()) if e.get('device_id')==device_id
        and e.get('platform')=='mungoon_soundbar' and not e.get('disabled_by')]

def ha_soundbar_control_device(entities, devices, device_id):
    if ha_soundbar_controls(entities,device_id):return device_id
    device=devices.get(device_id,{})
    identifiers={tuple(i) for i in device.get('identifiers',[])}
    linked=[did for did,d in devices.items() if not d.get('disabled_by') and ha_soundbar_controls(entities,did)
        and (d.get('parent_device_id',d.get('via_device_id'))==device_id or identifiers.intersection(tuple(i) for i in d.get('identifiers',[])))]
    if len(linked)==1:return linked[0]
    model=str(device.get('model') or '').upper().removeprefix('HW-')
    if not re.fullmatch(r'Q\d{3}[A-Z]',model) or not device.get('area_id'):return device_id
    matches=[did for did,d in devices.items() if did!=device_id and not d.get('disabled_by') and d.get('area_id')==device['area_id']
        and str(d.get('model') or '').upper().removeprefix('HW-')==model
        and any(e.get('device_id')==did and e.get('platform')=='smartthings' for e in entities.values())]
    return ha_soundbar_control_device(entities,devices,matches[0]) if len(matches)==1 else device_id

def ha_remote_pages(entities, devices, areas):
    """Group AV controls by assigned room; never assign unknown devices a room."""
    area_names={a['area_id']:a['name'] for a in areas};groups={}
    for eid,e in sorted(entities.items()):
        d=devices.get(e.get('device_id'),{})
        if e.get('disabled_by') or d.get('disabled_by') or e.get('entity_category'):continue
        remote=eid.startswith('remote.') and e.get('platform')=='androidtv_remote'
        if not remote and not eid.startswith('media_player.'):continue
        name=d.get('name_by_user') or d.get('name') or e.get('name') or eid
        model=d.get('model') or '';description=(name+' '+model).lower()
        sound='soundbar' in description or 'q990' in description
        tv=remote or e.get('device_class')=='tv' or any(x in description for x in (' tv','tv ','googletv','oled','s90d','s90f'))
        if not (sound or tv):continue
        area=e.get('area_id') or d.get('area_id');key=area or 'device:'+str(e.get('device_id') or eid)
        page=groups.setdefault(key,{'id':key,'name':area_names.get(area) or name,'remotes':[],'sounds':[],'media':[]})
        item={'entity':eid,'name':name,'model':model,'platform':e.get('platform'),'device_id':e.get('device_id')}
        page['remotes' if remote else 'sounds' if sound else 'media'].append(item)
    pages=[]
    for page in groups.values():
        remotes=page.pop('remotes');sounds=page.pop('sounds')
        if sounds:
            page['soundbar']=sorted(sounds,key=lambda s:s['platform']!='smartthings')[0]
            page['soundbar']['controls']=ha_soundbar_controls(entities,ha_soundbar_control_device(entities,devices,page['soundbar']['device_id']))
        # The Remote integration's media entity duplicates power/app state.
        page['media']=[m for m in page['media'] if m['platform']!='androidtv_remote']
        # Prefer actual TV controls over its duplicate Cast registration.
        if any(m['platform']=='smartthings' for m in page['media']):
            page['media']=[m for m in page['media'] if m['platform']!='cast' or 'streamer' in m['model'].lower()]
        if remotes:page['remote']=remotes[0]
        if remotes or page['media'] or sounds:pages.append(page)
        # Multiple navigation remotes in one room get separate pages.
        for remote in remotes[1:]:pages.append({'id':remote['entity'],'name':remote['name'],'remote':remote,'media':[]})
    return sorted(pages,key=lambda p:(p['id'].startswith('device:'),not bool(p.get('remote')),p['name'].casefold(),p['id']))

def ha_remotes_config(original, entities, devices, areas):
    dashboard=copy.deepcopy(original) if original is not None else {'title':'Remotes','mungoon_remotes_dashboard':True,'views':[{'title':'Remotes','path':'home','panel':True,'cards':[{'mungoon_remotes':True}]}]}
    if dashboard.get('mungoon_remotes_dashboard') is not True:raise HAError('The Remotes dashboard belongs to another editor. Nothing was overwritten.')
    cards=[c for v in dashboard.get('views',[]) for c in v.get('cards',[]) if c.get('mungoon_remotes') is True]
    if len(cards)!=1:raise HAError('The Remotes card could not be identified safely.')
    cards[0].update(type='custom:mungoon-media-remotes',pages=ha_remote_pages(entities,devices,areas))
    return dashboard

def ha_remotes_read(sync, ws, dashboards, entities, devices, areas):
    panels=[d for d in dashboards if d.get('url_path')==HA_REMOTES_DASHBOARD]
    if len(panels)>1:raise HAError('Duplicate Remotes dashboards.')
    original=None
    if panels:
        try:original=ws.call('lovelace/config',url_path=HA_REMOTES_DASHBOARD)
        except HAError:
            if ha_read(sync.data/'ha-remotes-dashboard.json',{}).get('id')!=panels[0].get('id'):raise
    ha_remotes_config(original,entities,devices,areas)
    return panels,original

def ha_remotes_save(sync, ws, dashboards, snapshot, entities, devices, areas):
    panels,original=snapshot
    config=ha_remotes_config(original,entities,devices,areas)
    if original is not None and ws.call('lovelace/config',url_path=HA_REMOTES_DASHBOARD)!=original:raise HAError('Remotes changed during sync; retrying with its latest contents.')
    if not panels:
        if any(d.get('url_path')==HA_REMOTES_DASHBOARD for d in ws.call('lovelace/dashboards/list')):raise HAError('Remotes was created during sync; retrying.')
        floor=next((d for d in dashboards if d.get('url_path')==HA_DASHBOARD),{})
        panel=ws.call('lovelace/dashboards/create',url_path=HA_REMOTES_DASHBOARD,title='Remotes',icon='mdi:remote-tv',show_in_sidebar=True,require_admin=bool(floor.get('require_admin',False)))
        ha_atomic(sync.data/'ha-remotes-dashboard.json',{'id':panel['id']})
    resources=ws.call('lovelace/resources')
    matching=[r for r in resources if r.get('url','').split('?')[0]==HA_REMOTES_RESOURCE.split('?')[0]]
    if len(matching)>1:raise HAError('Duplicate Remotes card resources.')
    if not matching:ws.call('lovelace/resources/create',res_type='module',url=HA_REMOTES_RESOURCE)
    elif matching[0].get('url')!=HA_REMOTES_RESOURCE:ws.call('lovelace/resources/update',resource_id=matching[0]['id'],res_type='module',url=HA_REMOTES_RESOURCE)
    if config!=original:
        if original is not None:ha_atomic(sync.data/'history/ha-remotes-before.json',original)
        ws.call('lovelace/config/save',url_path=HA_REMOTES_DASHBOARD,config=config)
        if ws.call('lovelace/config',url_path=HA_REMOTES_DASHBOARD)!=config:raise HAError('HA did not confirm Remotes; retrying.')
# END ANDROID TV CONTROLS

def ha_device_controls(placement, entities, camera_info=None):
    """Native HA controls, grouped by registry device rather than marker entity.

    Tile features test their own live supported_features in HA. Keeping the
    feature candidates here also handles entities that are offline during sync
    and capabilities that appear later, without freezing a state snapshot.
    Every enabled entity remains reachable through its native more-info dialog.
    """
    rich_features={
        'fan':['toggle','fan-preset-modes','fan-oscillate','fan-direction'],
        'light':['toggle','light-brightness','light-color-temp','light-effect'],
        'cover':['cover-open-close','cover-position','cover-tilt','cover-tilt-position'],
        'climate':['target-temperature','climate-hvac-modes','climate-preset-modes','climate-fan-modes','climate-swing-modes','climate-swing-horizontal-modes'],
        'humidifier':['humidifier-toggle','target-humidity','humidifier-modes'],
        'media_player':['media-player-playback','media-player-volume-slider','media-player-source','media-player-sound-mode'],
    }
    passive={'sensor','binary_sensor','event','device_tracker','weather','sun'}
    groups={name:[] for name in ('Controls','Settings','Readings','Diagnostics')}
    cards=[];info=(camera_info or {}).get(placement['device_id'],{})
    sound_device=placement.get('soundbar_control_device_id',placement['device_id'])
    rows=sorted((e for e in entities.values() if (e.get('device_id')==placement['device_id'] or
        e.get('device_id')==sound_device and e.get('platform')=='mungoon_soundbar') and not e.get('disabled_by')),
        key=lambda e:(e['entity_id']!=placement['entity_id'], e['entity_id']))
    for entity in rows:
        eid=entity['entity_id'];domain=eid.split('.')[0];category=entity.get('entity_category')
        if domain=='remote' and entity.get('platform')=='androidtv_remote' and not category:
            cards.append(ha_tv_remote(eid,entity.get('name') or entity.get('original_name') or 'TV remote'))
        elif domain=='camera':
            cards.append({'type':'picture-entity','entity':eid,'camera_view':'live',
                'show_name':True,'show_state':False,'tap_action':{'action':'more-info'}})
            if eid==placement['entity_id']:cards.extend(ha_camera_controls(eid,info))
        elif info.get('model')=='Tapo C210' and entity.get('platform')=='onvif' and (
            eid.endswith(('_autofocus','_wiper')) or
            eid.endswith('_ir_lamp') and eid not in info.get('known_switches',[])):
            # C210 has no lens wiper or adjustable focus. Its unknown generic
            # imaging switches must not imply controls the camera can perform.
            continue
        elif domain=='humidifier' and not category:
            # The native humidity dial shows current AND target humidity. A
            # generic tile can make the target look like the room reading and
            # hides the useful device-specific status behind more-info.
            cards.append({'type':'humidifier','entity':eid,
                'show_current_as_primary':True,
                'features':[{'type':'humidifier-toggle'},{'type':'humidifier-modes'}]})
        elif domain in rich_features and not category:
            card={'type':'tile','entity':eid,'tap_action':{'action':'more-info'},
                'icon_tap_action':{'action':'more-info'},'features_position':'bottom',
                'features':[{'type':feature} for feature in rich_features[domain]]}
            if eid==placement['entity_id'] and placement.get('icon'):card['icon']=placement['icon']
            cards.append(card)
            if domain=='fan':
                # Native speed features render a null percentage as 0%. A live
                # template reports unknown honestly and keeps native speed
                # controls reachable through the entity's more-info dialog.
                content=("{% set speed = state_attr('"+eid+"', 'percentage') %}"
                    "{% set mode = state_attr('"+eid+"', 'preset_mode') or state_attr('"+eid+"', 'mode') %}"
                    "{% if states('"+eid+"') in ['unknown', 'unavailable'] %}Speed: unavailable."
                    "{% elif speed is number %}Speed: {{ speed | round(0) | int }}%."
                    "{% elif mode %}Speed: not reported in {{ mode | replace('_', ' ') }} mode."
                    "{% else %}Speed: not reported.{% endif %}"
                    "{% if (state_attr('"+eid+"', 'supported_features') | int(0)) | bitwise_and(9) %}"
                    " Tap the fan name for speed controls."
                    "{% else %} Only fan power is available through this connection.{% endif %}")
                cards.append({'type':'markdown','content':content,'entity_id':[eid]})
        else:
            group='Diagnostics' if category=='diagnostic' or domain=='update' else 'Settings' if category=='config' else 'Readings' if domain in passive else 'Controls'
            # Native rows preserve domain-specific switches, selects, inputs,
            # buttons, update confirmations and the full more-info controls.
            row={'entity':eid}
            # A device page already supplies the device/room title. Prefer the
            # registry's control label so long room names do not hide what the
            # switch or reading actually does on a phone.
            label=entity.get('name') or entity.get('original_name')
            if isinstance(label,str) and label.strip():row['name']=label
            groups[group].append(row)
    for title,rows in groups.items():
        if rows:cards.append({'type':'entities','title':title,'show_header_toggle':False,'state_color':True,'entities':rows})
    if ha_soundbar_controls(entities,sound_device):
        cards.append({'type':'markdown','content':'Sound settings show the last command accepted by Samsung. Changes from the physical remote or SmartThings app are not reported. Choose a setting to apply it; a blank value means it has not been set here yet.'})
    cards.append({'type':'markdown','content':'Tap a control name for its full Home Assistant options. Use the back arrow to return to the floor plan.'})
    return {'type':'vertical-stack','mungoon_device_controls':placement['device_id'],'cards':cards}


def ha_device_views(dashboard, plan, entities, devices, camera_info=None):
    """Update only our identified control stacks; preserve other views/cards.

    Owned subviews remain when a marker is removed, just like its HA area. A
    later placement reuses the stable path and preserves any added user cards.
    Ambiguous ownership/path collisions fail before any HA registry mutation.
    """
    views=dashboard['views']
    floor=next(v for v in views if v.get('path')=='rooms')
    for p in plan.get('devices',[]):
        did=p['device_id'];path=ha_device_path(did)
        matching=[v for v in views if v.get('path')==path or v.get('mungoon_device_id')==did]
        if len(matching)>1:raise HAError('Duplicate device control pages in HA. No dashboard was replaced.')
        title=plan.get('ha_device_names',{}).get(did) or p.get('ha_name') or devices[did].get('name_by_user') or p.get('name') or devices[did].get('name') or p['entity_id']
        controls_placement={**p,'soundbar_control_device_id':ha_soundbar_control_device(entities,devices,did)}
        stack=ha_device_controls(controls_placement,entities,camera_info)
        if matching:
            view=matching[0]
            if view.get('path')!=path or view.get('mungoon_device_id')!=did:raise HAError('A device control page conflicts with an existing HA view. No dashboard was replaced.')
            cards=view.get('cards',[]);indexes=[i for i,c in enumerate(cards) if c.get('mungoon_device_controls')==did]
            if len(indexes)!=1:raise HAError('The device control stack could not be identified safely. No dashboard was replaced.')
            cards[indexes[0]]=stack;view['title']=title
        else:
            view={'title':title,'path':path,'subview':True,'back_path':'/'+HA_DASHBOARD+'/rooms',
                'mungoon_device_id':did,'cards':[stack]}
            if floor.get('theme'):view['theme']=floor['theme']
            views.append(view)


def ha_card(plan, entities):
    svg,(x,y,w,h)=ha_floor_svg(plan)
    image='data:image/svg+xml;base64,'+base64.b64encode(svg.encode()).decode()
    elements=[]
    for d in plan.get('devices',[]):
        if d['entity_id'] not in entities: continue
        # Tap opens the whole physical device; hold keeps quick entity controls.
        elements.append({'type':'state-icon','entity':d['entity_id'],'title':d.get('name') or d['entity_id'], 'state_color':True,
            'tap_action':{'action':'navigate','navigation_path':'/'+HA_DASHBOARD+'/'+ha_device_path(d['device_id'])},'hold_action':{'action':'more-info'},
            'style':{'left':f'{(d["x"]-x)/w*100:.4f}%','top':f'{(d["y"]-y)/h*100:.4f}%',
                'color':'#b7d3d8','--paper-item-icon-color':'#b7d3d8','--state-icon-color':'#b7d3d8',
                '--mdc-icon-size':'clamp(14px, 4vw, 22px)',
                'width':'44px','height':'44px','padding':'0','line-height':'0',
                'display':'flex','align-items':'center','justify-content':'center',
                'transform':'translate(-50%, -50%)','transform-origin':'center center',
                'background':'transparent','border':'0'}})
        if d.get('icon'): elements[-1]['icon']=d['icon']
    return {'type':'picture-elements','image':image,'dark_mode_image':image,'dark_mode_filter':'none','elements':elements,'mungoon_floor_plan':True}

def ha_room_environment(plan, entities, devices, states, mapping):
    """References only: readings are supplied live by the signed-in HA frontend."""
    metrics={'temperature','humidity','pm25','pm10','aqi','carbon_dioxide',
             'volatile_organic_compounds','volatile_organic_compounds_parts','illuminance'}
    result={r['id']:[] for r in plan['rooms']}
    area_rooms={area:rid for rid,area in mapping.items() if rid in result}
    for eid,e in sorted(entities.items()):
        d=devices.get(e.get('device_id'),{})
        if not eid.startswith('sensor.') or e.get('disabled_by') or d.get('disabled_by') or e.get('entity_category'):continue
        attrs=states.get(eid,{}).get('attributes',{})
        metric=e.get('device_class') or attrs.get('device_class') or e.get('original_device_class')
        # VeSync qualitative air quality has no HA device class.
        if not metric and eid.endswith('_air_quality'):metric='air_quality'
        if metric not in metrics|{'air_quality'}:continue
        rid=area_rooms.get(e.get('area_id') or d.get('area_id'))
        if rid is None:continue
        source=d.get('name_by_user') or d.get('name') or e.get('name') or attrs.get('friendly_name') or eid
        result[rid].append({'entity_id':eid,'metric':metric,'source':source,
            'name':e.get('name') or attrs.get('friendly_name') or e.get('original_name') or eid,
            'unit':attrs.get('unit_of_measurement') or '', 'platform':e.get('platform') or ''})
    # All references remain available in details; prefer direct integrations for
    # the summary instead of repeating Matter/cloud copies of the same reading.
    priority={'zha':0,'zwave_js':0,'tplink':0,'vesync':0,'tuya':1,'matter':2,'smartthings':3}
    for rows in result.values():rows.sort(key=lambda s:(priority.get(s['platform'],1),s['source'].casefold(),s['entity_id']))
    return result


def ha_interactive_card(plan, entities, environment=None, movement=None):
    saved={k:copy.deepcopy(plan[k]) for k in ('rooms','items','walls','devices','ha_controls','north_bearing','north_reference') if k in plan}
    saved['devices']=[d for d in saved.get('devices',[]) if d['entity_id'] in entities]
    if 'ha_controls' in saved:
        saved['ha_controls']=[c for c in saved['ha_controls'] if c['entity_id'] in entities and not entities[c['entity_id']].get('disabled_by')]
    return {'type':'custom:mungoon-floor-card','plan':saved,'room_environment':environment or {},'movement':movement or {},
        'room_paths':{r['id']:'/'+HA_DASHBOARD+'/'+ha_room_path(r['id']) for r in saved['rooms']},
        'device_paths':{d['device_id']:'/'+HA_DASHBOARD+'/'+ha_device_path(d['device_id']) for d in saved['devices']},'mungoon_floor_plan':True}


def ha_room_path(rid):
    return 'room-'+hashlib.sha256(rid.encode()).hexdigest()[:16]


def ha_room_views(dashboard,plan,entities,devices,mapping,environment,camera_info=None,movement=None):
    """Owned room pages: same map, stable navigation and native device controls."""
    views=dashboard['views'];all_devices=ha_all_device_plan(plan,entities,devices)['devices']
    for room in plan['rooms']:
        rid=room['id'];path=ha_room_path(rid);title=room.get('ha_name') or room['name']
        matches=[v for v in views if v.get('path')==path or v.get('mungoon_room_id')==rid]
        if len(matches)>1:raise HAError('Duplicate room pages in HA. No dashboard was replaced.')
        existing=matches[0] if matches else None
        if existing:
            if existing.get('path')!=path or existing.get('mungoon_room_id')!=rid:raise HAError('A room page conflicts with an existing HA view. No dashboard was replaced.')
            indexes=[i for i,c in enumerate(existing.get('cards',[])) if c.get('mungoon_room_controls')==rid]
            if len(indexes)!=1:raise HAError('The room controls could not be identified safely. No dashboard was replaced.')
        card=ha_interactive_card(plan,entities,environment,movement);card['focus_room']=rid
        area=mapping.get(rid)
        selected={eid:e for eid,e in entities.items() if area and not e.get('disabled_by') and not devices.get(e.get('device_id'),{}).get('disabled_by') and (e.get('area_id') or devices.get(e.get('device_id'),{}).get('area_id'))==area}
        content=[card]
        for device in all_devices:
            did=device['device_id'];rows={eid:e for eid,e in selected.items() if e.get('device_id')==did}
            if not rows:continue
            name=devices[did].get('name_by_user') or devices[did].get('name') or 'Device'
            content.append({'type':'heading','heading':name,'heading_style':'subtitle'})
            controls=ha_device_controls(device,rows,camera_info)
            controls['cards'][-1]={'type':'markdown','content':'Tap a control name for its full Home Assistant options.'}
            content.append(controls)
        orphan=[{'entity':eid} for eid,e in selected.items() if not e.get('device_id')]
        if orphan:content.append({'type':'entities','title':'Room readings and helpers','show_header_toggle':False,'entities':orphan})
        if len(content)==1:content.append({'type':'markdown','content':'No enabled devices or sensors are assigned to this room in Home Assistant.'})
        stack={'type':'vertical-stack','mungoon_room_controls':rid,'cards':content}
        if existing:existing['cards'][indexes[0]]=stack;existing['title']=title
        else:views.append({'title':title,'path':path,'subview':True,'back_path':'/'+HA_DASHBOARD+'/rooms','mungoon_room_id':rid,'panel':True,'cards':[stack]})


HA_DEVICES_DASHBOARD = 'devices-browser'

def ha_device_catalog(devices, entities, areas):
    """Registry metadata and entity references only; live states stay in HA."""
    area_names={a['area_id']:a['name'] for a in areas}
    software={'Home Assistant Core','Home Assistant Supervisor','Home Assistant Host','Home Assistant Operating System','Matter Server','Sun','Backup','Forecast','Google Translate en com'}
    rows=[]
    for did,d in devices.items():
        # User-disabled copies remain recoverable in HA settings, not in the
        # everyday device navigator or the editor's active device list.
        if d.get('disabled_by'):continue
        identifiers={tuple(i) for i in d.get('identifiers',[])}
        if ha_soundbar_controls(entities,did) and any(other!=did and identifiers.intersection(tuple(i) for i in device.get('identifiers',[]))
            and any(e.get('device_id')==other and e.get('platform')=='smartthings' for e in entities.values()) for other,device in devices.items()):continue
        sound_device=ha_soundbar_control_device(entities,devices,did)
        all_entities=[e for e in entities.values() if e.get('device_id')==did or e.get('device_id')==sound_device and e.get('platform')=='mungoon_soundbar']
        enabled=[e for e in all_entities if not e.get('disabled_by')]
        name=d.get('name_by_user') or d.get('name') or d.get('model') or 'Unnamed device'
        rows.append({'id':did,'name':name,'model':d.get('model') or '',
            'manufacturer':d.get('manufacturer') or '',
            'room_id':d.get('area_id') if d.get('area_id') in area_names else '',
            'room':area_names.get(d.get('area_id'),'Unassigned'),
            'integrations':sorted({e['platform'] for e in all_entities if e.get('platform')}),
            'entity_ids':sorted(e['entity_id'] for e in enabled),
            'disabled_count':len(all_entities)-len(enabled),
            'kind':'Service / software' if name in software or d.get('model') in software else ('Scene record' if d.get('model')=='Tuya Scene' or d.get('model')=='TuyaScene' else ('Service record' if d.get('entry_type')=='service' else 'Device record')),
            'device_disabled':bool(d.get('disabled_by')),
            'path':'/'+HA_DEVICES_DASHBOARD+'/'+ha_device_path(did)})
    return sorted(rows,key=lambda r:(r['name'].casefold(),r['id']))

def ha_all_device_plan(plan, entities, devices):
    placed={p['device_id']:p for p in plan.get('devices',[])}
    result={**plan,'devices':[]}
    for did in devices:
        enabled=sorted((e for e in entities.values() if e.get('device_id')==did and not e.get('disabled_by')),
            key=lambda e:(not e['entity_id'].startswith('camera.'),bool(e.get('entity_category')),e['entity_id']))
        # Control metadata is transient; it must never change saved markers or their revision.
        result['devices'].append(copy.deepcopy(placed.get(did) or {'device_id':did,'entity_id':enabled[0]['entity_id'] if enabled else ''}))
    return result

def ha_devices_config(original, plan, entities, devices, areas, camera_info):
    """Preserve user additions, stable pages and unrelated dashboard metadata."""
    if original is None:
        dashboard={'title':'Devices','mungoon_devices_dashboard':True,'views':[{'title':'All devices','path':'devices','panel':True,'cards':[{'mungoon_device_navigator':True}]}]}
    else:
        if original.get('mungoon_devices_dashboard') is not True:raise HAError('The Devices dashboard path is already used by another dashboard. Nothing was overwritten.')
        dashboard=copy.deepcopy(original)
    roots=[v for v in dashboard['views'] if v.get('path')=='devices']
    if len(roots)!=1:raise HAError('The Devices navigator view could not be identified safely.')
    root=roots[0];indexes=[i for i,c in enumerate(root.get('cards',[])) if c.get('mungoon_device_navigator')]
    if len(indexes)!=1:raise HAError('The Devices navigator card could not be identified safely.')
    root['cards'][indexes[0]]={'type':'custom:mungoon-device-navigator','mungoon_device_navigator':True,
        'devices':ha_device_catalog(devices,entities,areas)}
    # Reuse the established, guarded control-stack builder without altering the
    # Floor Plan dashboard or introducing registry area/name mutations.
    placeholder={'path':'rooms'};dashboard['views'].insert(0,placeholder)
    all_plan=ha_all_device_plan(plan,entities,devices)
    for placement in all_plan['devices']:
        placement['soundbar_control_device_id']=ha_soundbar_control_device(entities,devices,placement['device_id'])
    ha_device_views(dashboard,all_plan,entities,devices,camera_info)
    dashboard['views'].remove(placeholder)
    for view in dashboard['views']:
        did=view.get('mungoon_device_id')
        if did in devices:
            view['back_path']='/'+HA_DEVICES_DASHBOARD+'/devices'
            owned=next(c for c in view['cards'] if c.get('mungoon_device_controls')==did)
            d=devices[did];count=sum(e.get('device_id')==did and not e.get('disabled_by') for e in entities.values())
            metadata=' | '.join(html.escape(str(v)) for v in (d.get('manufacturer'),d.get('model')) if v)
            if not count:metadata+='\n\nNo enabled entities are exposed for this registry record. There are no live controls or readings to display.'
            owned['cards'].insert(0,{'type':'markdown','content':metadata or 'Device registry record.'})
            owned['cards'][-1]={'type':'markdown','content':'Tap a control name for its full Home Assistant options. Use the back arrow to return to Devices.'}
    ha_tv_view(dashboard,entities,devices)
    return dashboard



class HomeAssistantSync:
    def __init__(self, data, websocket=HAWebSocket):
        self.data=Path(data);self.private=self.data/'private';self.credentials=self.private/'home-assistant.json'
        self.meta=self.data/'ha-floor-sync.json';self.job=self.data/'ha-floor-job.json'
        self.lock=threading.RLock();self.auth_lock=threading.Lock();self.run_lock=threading.Lock();self.wake=threading.Event();self.websocket=websocket
        self.started=False

    def status(self):
        with self.lock:
            state=ha_read(self.meta, {})
            return {'connected':self.credentials.exists(),'url':HA_URL,'dashboard':HA_URL+'/floor-plan/rooms',
                **{k:state[k] for k in ('state','error','synced_at','synced_revision','requested_revision','rooms','device_count','warnings') if k in state}}

    def begin_auth(self, email):
        state=secrets.token_urlsafe(32)
        with self.lock: ha_atomic(self.private/'ha-oauth-state.json',{'state':state,'email':email,'expires':time.time()+600})
        return {'url':HA_URL+'/auth/authorize?'+urllib.parse.urlencode({'client_id':HA_CLIENT,'redirect_uri':HA_REDIRECT,'state':state})}

    def token_request(self, fields):
        request=urllib.request.Request(HA_URL+'/auth/token',data=urllib.parse.urlencode(fields).encode(),headers={'Content-Type':'application/x-www-form-urlencoded'})
        try:
            with urllib.request.urlopen(request,timeout=15) as response: value=json.load(response)
            if not isinstance(value.get('access_token'),str): raise ValueError()
            return value
        except Exception: raise HAError('HA sign-in could not be renewed. Reconnect Home Assistant.') from None

    def finish_auth(self, email, state, code):
        with self.auth_lock:
            pending=ha_read(self.private/'ha-oauth-state.json',{})
            if not isinstance(state,str) or not secrets.compare_digest(pending.get('state',''),state) or pending.get('email')!=email or pending.get('expires',0)<time.time() or not isinstance(code,str) or not 1<=len(code)<=2048: raise HAError('This connection request expired. Select Connect HA again.')
            (self.private/'ha-oauth-state.json').unlink(missing_ok=True)
            value=self.token_request({'grant_type':'authorization_code','code':code,'client_id':HA_CLIENT})
            if not value.get('refresh_token'): raise HAError('Home Assistant did not provide a renewable connection.')
            with self.websocket(value['access_token']) as ws:
                user=ws.call('auth/current_user')
                if not user.get('is_admin'): raise HAError('Connect with a Home Assistant administrator to sync room assignments.')
            value['expires_at']=time.time()+value.get('expires_in',1800)-60
            with self.lock: ha_atomic(self.credentials,value)
        return self.status()

    def token(self):
        with self.auth_lock:
            value=ha_read(self.credentials,{})
            if not value: raise HAError('Connect Home Assistant first.')
            if value.get('expires_at',0)<time.time():
                result=self.token_request({'grant_type':'refresh_token','refresh_token':value['refresh_token'],'client_id':HA_CLIENT})
                value.update(result);value['expires_at']=time.time()+result.get('expires_in',1800)-60
                with self.lock: ha_atomic(self.credentials,value)
            return value['access_token']

    def map_context(self):
        with self.websocket(self.token()) as ws:config=ws.call('get_config')
        return {k:config.get(k) for k in ('latitude','longitude','elevation','time_zone')}

    def camera_controls_info(self,plan,entities,devices,ws):
        """Read redacted diagnostics; retain no credentials or stream URLs."""
        cameras=[p for p in plan.get('devices',[]) if p['entity_id'].startswith('camera.')
            and entities[p['entity_id']].get('platform')=='onvif']
        if not cameras:return {}
        states={s['entity_id']:s for s in ws.call('get_states')}
        result={}
        for p in cameras:
            did=p['device_id'];entity=entities[p['entity_id']]
            info={'model':devices[did].get('model'),
                'known_switches':[eid for eid,e in entities.items() if e.get('device_id')==did
                    and states.get(eid,{}).get('state') in ('on','off')]}
            result[did]=info
            entry=entity.get('config_entry_id','')
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',entry):continue
            request=urllib.request.Request(HA_URL+'/api/diagnostics/config_entry/'+entry,
                headers={'Authorization':'Bearer '+self.token()})
            try:
                with urllib.request.urlopen(request,timeout=8) as response:diagnostics=json.load(response)
                device=diagnostics.get('data',{}).get('device',{})
                profiles=device.get('profiles',[])
                if not device.get('capabilities',{}).get('ptz') or not profiles:continue
                # Only promise capabilities shared by the exposed profiles.
                info['relative']=all(q.get('ptz',{}).get('relative') for q in profiles)
                info['continuous']=all(q.get('ptz',{}).get('continuous') for q in profiles)
                presets=set(str(t) for t in profiles[0].get('ptz',{}).get('presets',[]))
                for q in profiles[1:]:presets.intersection_update(str(t) for t in q.get('ptz',{}).get('presets',[]))
                info['presets']=sorted(t for t in presets if re.fullmatch(r'[A-Za-z0-9_.-]{1,80}',t))[:20]
            except (OSError,ValueError,TypeError,KeyError):
                # Video remains available when optional diagnostics cannot load;
                # never invent PTZ support or leak request/credential details.
                continue
        return result

    def ensure_card_resource(self,ws):
        resources=ws.call('lovelace/resources')
        matches=[r for r in resources if r.get('url','').split('?')[0]==HA_CARD_RESOURCE.split('?')[0]]
        if len(matches)>1:raise HAError('Duplicate Mungoon floor-plan resources. No dashboard was replaced.')
        if not matches:ws.call('lovelace/resources/create',res_type='module',url=HA_CARD_RESOURCE)
        elif matches[0].get('url')!=HA_CARD_RESOURCE or matches[0].get('type')!='module':ws.call('lovelace/resources/update',resource_id=matches[0]['id'],res_type='module',url=HA_CARD_RESOURCE)

    def inventory(self):
        with self.websocket(self.token()) as ws:
            areas=ws.call('config/area_registry/list');devices=ws.call('config/device_registry/list');entities=ws.call('config/entity_registry/list');states=ws.call('get_states')
        states={s['entity_id']:s for s in states}
        platforms={d['id']:sorted({e['platform'] for e in entities if e.get('device_id')==d['id'] and e.get('platform')}) for d in devices}
        return {'areas':[{'area_id':a['area_id'],'name':a['name']} for a in areas],
            'devices':[{**{k:d.get(k) for k in ('id','name','name_by_user','model','manufacturer','area_id','disabled_by','entry_type')},'integrations':platforms[d['id']]} for d in devices],
            'entities':[{'entity_id':e['entity_id'],'device_id':e.get('device_id'),'area_id':e.get('area_id'), 'disabled_by':e.get('disabled_by'),'entity_category':e.get('entity_category'),
                'name':e.get('name') or states.get(e['entity_id'],{}).get('attributes',{}).get('friendly_name') or e.get('original_name') or e['entity_id'],
                'state':states.get(e['entity_id'],{}).get('state','unavailable'),
                'platform':e.get('platform') or '',
                'device_class':e.get('device_class') or states.get(e['entity_id'],{}).get('attributes',{}).get('device_class') or e.get('original_device_class'),
                'unit':states.get(e['entity_id'],{}).get('attributes',{}).get('unit_of_measurement') or ''} for e in entities]}

    def sheet_reference(self):
        return ha_read(self.data/'ha-device-sheet.json',{'tabs':[],'device_notes':{},'source_url':'','imported_at':None})

    def import_sheet_reference(self,value):
        """Preserve the former workbook as owner-only reference data, outside releases."""
        if not isinstance(value,dict) or set(value)-{'tabs','device_notes','source_url','imported_at'}:raise ValueError('Invalid workbook reference.')
        tabs=value.get('tabs');notes=value.get('device_notes',{})
        if not isinstance(tabs,list) or len(tabs)>12 or not isinstance(notes,dict) or len(notes)>1000:raise ValueError('Invalid workbook reference.')
        for tab in tabs:
            if not isinstance(tab,dict) or set(tab)!={'title','rows'} or not isinstance(tab['title'],str) or not 1<=len(tab['title'])<=100 or not isinstance(tab['rows'],list) or len(tab['rows'])>200:raise ValueError('Invalid workbook tab.')
            for row in tab['rows']:
                if not isinstance(row,list) or len(row)>30 or any(not isinstance(c,str) or len(c)>5000 for c in row):raise ValueError('Invalid workbook cells.')
        if any(not isinstance(k,str) or len(k)>150 or not isinstance(v,str) or len(v)>5000 for k,v in notes.items()):raise ValueError('Invalid device notes.')
        url=value.get('source_url','')
        if not isinstance(url,str) or len(url)>500 or url and not re.fullmatch(r'https://docs\.google\.com/spreadsheets/d/[A-Za-z0-9_-]+/edit',url):raise ValueError('Invalid workbook source.')
        result={'tabs':tabs,'device_notes':notes,'source_url':url,'imported_at':time.time()}
        with self.lock:
            old=self.sheet_reference()
            if old['tabs']:ha_atomic(self.data/'history'/('ha-device-sheet-'+str(time.time_ns())+'.json'),old)
            ha_atomic(self.data/'ha-device-sheet.json',result)
        return result

    def enqueue(self, plan, revision):
        if not self.credentials.exists(): return self.status()
        ha_validate(plan)
        with self.lock:
            ha_atomic(self.job,{'plan':plan,'revision':revision})
            state=ha_read(self.meta,{})
            state.update(state='pending',error='',requested_revision=revision)
            ha_atomic(self.meta,state)
        self.wake.set()
        return self.status()

    def start(self, store_lock=None, read_state=None, save_state=None):
        if self.started: return
        self.started=True
        threading.Thread(target=self.worker,daemon=True,name='ha-floor-sync').start()
        if store_lock is not None and read_state and save_state:
            self.move_store=(store_lock,read_state,save_state)
            threading.Thread(target=self.move_worker,daemon=True,name='ha-floor-moves').start()

    def handle_move(self, event, user):
        # HA supplies context.user_id itself. Never trust a claimed user in data.
        if not user.get('is_admin') or not user.get('id') or event.get('context',{}).get('user_id')!=user['id']:
            raise HAError('Only the HA account connected by the Studio owner can move devices.')
        request=event.get('data')
        if not isinstance(request,dict) or len(ha_json(request))>2048:raise HAError('Invalid device move.')
        lock,read,save=self.move_store
        with lock:
            plan,revision=read('floor-plan')
            if revision!=ha_revision(plan):raise HAError('The saved layout could not be verified.')
            receipts_path=self.data/'ha-floor-move-receipts.json'
            receipts=ha_read(receipts_path,[])
            previous=next((r for r in receipts if r['request'].get('request_id')==request.get('request_id')),None)
            if previous:
                if previous['request']!=request:raise HAError('This move reference was already used. Refresh and try again.')
                return previous['result']
            updated,marker,room=ha_move_device(plan,request)
            # Re-check registry membership before saving, including disabled copies.
            with self.websocket(self.token()) as ws:
                current=ws.call('auth/current_user')
                if not current.get('is_admin') or current.get('id')!=user['id']:raise HAError('Reconnect the owner HA account before moving devices.')
                devices=ws.call('config/device_registry/list');entities=ws.call('config/entity_registry/list')
            device=next((d for d in devices if d['id']==marker['device_id']),None)
            entity=next((e for e in entities if e['entity_id']==marker['entity_id'] and e.get('device_id')==marker['device_id']),None)
            if not device or device.get('disabled_by') or not entity or entity.get('disabled_by'):raise HAError('This device is unavailable for placement. Refresh devices in Layout Studio.')
            revision=save('floor-plan',updated)
            self.enqueue(updated,revision)
            result={'request_id':request['request_id'],'saved':True,'revision':revision,
                'device':marker,'room':room.get('ha_name') or room['name'] if room else '',
                'message':'Saved to Layout Studio on NAS. HA is updating.'}
            ha_atomic(receipts_path,(receipts+[{'request':request,'result':result}])[-100:])
            return result

    def move_worker(self):
        while True:
            try:
                with self.websocket(self.token()) as ws:
                    user=ws.call('auth/current_user')
                    if not user.get('is_admin') or not user.get('id'):raise HAError('HA administrator required.')
                    ws.call('subscribe_events',event_type=HA_MOVE_EVENT)
                    while True:
                        if not select.select([ws.sock],[],[],30)[0]:
                            ws.frame(b'',9);continue
                        message=ws.receive()
                        if message.get('type')!='event':continue
                        event=message.get('event',{})
                        if event.get('event_type')!=HA_MOVE_EVENT:continue
                        request=event.get('data',{})
                        request_id=request.get('request_id') if isinstance(request,dict) else None
                        if not isinstance(request_id,str) or not re.fullmatch(r'[a-zA-Z0-9-]{16,80}',request_id):continue
                        try:result=self.handle_move(event,user)
                        except Exception as error:
                            result={'request_id':request_id,'saved':False,'error':str(error) if isinstance(error,HAError) else 'Move could not be saved. Refresh and try again.'}
                        # Separate connection: replies must not consume queued move events.
                        with self.websocket(self.token()) as reply:reply.call('fire_event',event_type=HA_MOVE_RESULT,event_data=result)
            except Exception:
                threading.Event().wait(3)

    def worker(self):
        while True:
            self.wake.wait(30);self.wake.clear()
            if not self.credentials.exists() or not self.job.exists(): continue
            with self.run_lock:
                with self.lock: job=ha_read(self.job,{});state=ha_read(self.meta,{})
                # Resend after a server/code release as well as after each design save.
                if not job: continue
                try:
                    with self.lock:
                        state.update(state='syncing',error='');ha_atomic(self.meta,state)
                    result=self.sync(job['plan'])
                    with self.lock:
                        state=ha_read(self.meta,{})
                        state.update(result,state='synced',error='',synced_at=time.time(),synced_revision=job['revision'])
                        latest=ha_read(self.job,{})
                        if latest.get('revision')==job['revision']: self.job.unlink(missing_ok=True)
                        else: state['state']='pending';self.wake.set()
                        ha_atomic(self.meta,state)
                except Exception as error:
                    with self.lock:
                        state=ha_read(self.meta,{})
                        state.update(state='error',error=str(error) if isinstance(error,HAError) else 'HA is unavailable. Your NAS layout is safe; sync retries automatically.')
                        ha_atomic(self.meta,state)

    def sync(self, plan):
        ha_validate(plan);names=ha_area_names(plan)
        with self.websocket(self.token()) as ws:
            areas=ws.call('config/area_registry/list');devices=ws.call('config/device_registry/list');entities=ws.call('config/entity_registry/list')
            dashboard=ws.call('lovelace/config',url_path=HA_DASHBOARD)
            original=copy.deepcopy(dashboard)
            dashboards=ws.call('lovelace/dashboards/list')
            device_panels=[d for d in dashboards if d.get('url_path')==HA_DEVICES_DASHBOARD]
            if len(device_panels)>1:raise HAError('Duplicate Devices dashboards. No dashboard was replaced.')
            devices_original=None
            if device_panels:
                try:devices_original=ws.call('lovelace/config',url_path=HA_DEVICES_DASHBOARD)
                except HAError:
                    ownership=ha_read(self.data/'ha-devices-dashboard.json',{})
                    if ownership.get('id')!=device_panels[0].get('id'):raise
            views=[v for v in dashboard.get('views',[]) if v.get('path')=='rooms']
            if len(views)!=1: raise HAError('The Floor Plan dashboard needs one Rooms view. No dashboard was replaced.')
            view=views[0];cards=view.get('cards',[])
            matches=[i for i,c in enumerate(cards) if c.get('mungoon_floor_plan')]
            if not matches and len(cards)==1 and cards[0].get('type')=='picture' and cards[0].get('image','').startswith('data:image/svg+xml'): matches=[0]
            if len(matches)!=1: raise HAError('The existing floor-plan card could not be identified safely. No dashboard was replaced.')
            backup=self.data/'history/ha-floor-dashboard-before.json'
            if not backup.exists(): ha_atomic(backup,original)
            entity_map={e['entity_id']:e for e in entities};device_map={d['id']:d for d in devices}
            name_updates=dict(plan.get('ha_device_names',{}))
            for p in plan.get('devices',[]):
                if p.get('ha_name'):name_updates.setdefault(p['device_id'],p['ha_name'])
            if any(did not in device_map for did in name_updates):raise HAError('A named device was removed from HA. Refresh the Device Sheet before saving.')
            # Preflight all placements before changing areas or any assignments.
            for p in plan.get('devices',[]):
                e=entity_map.get(p['entity_id']);d=device_map.get(p['device_id'])
                if not d or d.get('disabled_by') or not e or e.get('disabled_by') or e.get('device_id')!=p['device_id']: raise HAError('A placed device or entity was removed or disabled in HA. Refresh devices and remove or replace its marker.')
            all_plan=ha_all_device_plan(plan,entity_map,device_map)
            camera_info=self.camera_controls_info(all_plan,entity_map,device_map,ws)
            # Ownership/path preflight before registry or dashboard mutations.
            remotes_snapshot=ha_remotes_read(self,ws,dashboards,entity_map,device_map,areas)
            ha_tv_view(dashboard,entity_map,device_map)
            ha_devices_config(devices_original,plan,entity_map,device_map,areas,camera_info)
            ha_device_views(dashboard,plan,entity_map,device_map,camera_info)
            ha_room_views(dashboard,plan,entity_map,device_map,{}, {},camera_info)
            self.ensure_card_resource(ws)
            mapping_file=self.data/'ha-room-map.json'
            mapping=ha_read(mapping_file,{})
            by_id={a['area_id']:a for a in areas}
            used=set()
            for room in plan['rooms']:
                rid=room['id'];name=names[rid];area=by_id.get(mapping.get(rid))
                same=[a for a in by_id.values() if a['name'].casefold()==name.casefold() and (not area or a['area_id']!=area['area_id'])]
                if area and area['name']!=name:
                    if same: raise HAError('An HA area already has the new room name. Choose a unique Home Assistant room name.')
                    area=ws.call('config/area_registry/update',area_id=area['area_id'],name=name)
                elif not area:
                    if len(same)>1: raise HAError('Multiple HA areas match a room name. Rename the room uniquely.')
                    area=same[0] if same else ws.call('config/area_registry/create',name=name)
                if area['area_id'] in used: raise HAError('Two floor rooms map to the same HA area.')
                used.add(area['area_id']);mapping[rid]=area['area_id'];by_id[area['area_id']]=area
                # Persist after every acknowledged creation, so retries never duplicate rooms.
                ha_atomic(mapping_file,mapping)
            warnings=[]
            for p in plan.get('devices',[]):
                # A marker outside the rooms is still part of the map. Only a
                # placement inside a room changes the device/entity HA areas.
                room=ha_room(plan,p)
                if room is None: continue
                area_id=mapping[room['id']];d=device_map[p['device_id']]
                changes={}
                if d.get('area_id')!=area_id: changes['area_id']=area_id
                if changes:
                    updated=ws.call('config/device_registry/update',device_id=d['id'],**changes)
                    if any(updated.get(k)!=v for k,v in changes.items()): raise HAError('HA did not confirm a device name or room assignment.')
                for e in entities:
                    if e.get('device_id')==d['id'] and e.get('area_id') and e['area_id']!=area_id:
                        # Explicit overrides must follow the placed device, too.
                        updated=ws.call('config/entity_registry/update',entity_id=e['entity_id'],area_id=area_id)
                        if updated.get('entity_entry',updated).get('area_id')!=area_id: raise HAError('HA did not confirm an entity room override.')
            for did,name in name_updates.items():
                if device_map[did].get('name_by_user')!=name:
                    updated=ws.call('config/device_registry/update',device_id=did,name_by_user=name)
                    if updated.get('name_by_user')!=name:raise HAError('HA did not confirm a device name. Sync will retry.')
            fresh_devices={d['id']:d for d in ws.call('config/device_registry/list')}
            fresh_entities={e['entity_id']:e for e in ws.call('config/entity_registry/list')}
            states={s['entity_id']:s for s in ws.call('get_states')}
            environment=ha_room_environment(plan,fresh_entities,fresh_devices,states,mapping)
            user=ws.call('auth/current_user')
            movement={'user_id':user.get('id'),'revision':ha_revision(plan),'event':HA_MOVE_EVENT,'reply_event':HA_MOVE_RESULT} if user.get('is_admin') and user.get('id') else {}
            cards[matches[0]]=ha_interactive_card(plan,entity_map,environment,movement)
            ha_room_views(dashboard,plan,fresh_entities,fresh_devices,mapping,environment,camera_info,movement)
            fresh_areas=ws.call('config/area_registry/list')
            devices_dashboard=ha_devices_config(devices_original,plan,entity_map,fresh_devices,fresh_areas,camera_info)
            if devices_original is not None and ws.call('lovelace/config',url_path=HA_DEVICES_DASHBOARD)!=devices_original:
                raise HAError('The Devices dashboard changed during sync. Retrying with its latest contents.')
            if ws.call('lovelace/config',url_path=HA_DASHBOARD)!=original: raise HAError('The HA dashboard changed during sync. Retrying with its latest contents.')
            if dashboard!=original:
                ws.call('lovelace/config/save',url_path=HA_DASHBOARD,config=dashboard)
                if ws.call('lovelace/config',url_path=HA_DASHBOARD)!=dashboard: raise HAError('HA did not confirm the updated floor plan. Retrying automatically.')
            if not device_panels:
                floor_panel=next((d for d in dashboards if d.get('url_path')==HA_DASHBOARD),{})
                created=ws.call('lovelace/dashboards/create',url_path=HA_DEVICES_DASHBOARD,title='Devices',icon='mdi:devices',show_in_sidebar=True,require_admin=bool(floor_panel.get('require_admin',False)))
                ha_atomic(self.data/'ha-devices-dashboard.json',{'id':created['id']})
            if devices_dashboard!=devices_original:
                ws.call('lovelace/config/save',url_path=HA_DEVICES_DASHBOARD,config=devices_dashboard)
                if ws.call('lovelace/config',url_path=HA_DEVICES_DASHBOARD)!=devices_dashboard:raise HAError('HA did not confirm the Devices dashboard. Retrying automatically.')
            ha_remotes_save(self,ws,dashboards,remotes_snapshot,fresh_entities,fresh_devices,fresh_areas)
            return {'rooms':{rid:{'area_id':mapping[rid],'name':name} for rid,name in names.items()},'device_count':len(plan.get('devices',[])),'warnings':warnings}
