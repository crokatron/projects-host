"""Owner-only visual automation builder. Validated actions and conflict-safe saves."""
import copy, hashlib, json, math, re, threading, time, urllib.error, urllib.request
from home_assistant import HAError, HA_URL, ha_json, ha_atomic, ha_read

class AutomationConflict(ValueError):
    pass

AUTOMATION_ACTIONS = {
    'turn_on': ('turn_on', ('light','switch','fan','humidifier','climate','input_boolean','media_player'), None),
    'turn_off': ('turn_off', ('light','switch','fan','humidifier','climate','input_boolean','media_player'), None),
    'toggle': ('toggle', ('light','switch','fan','humidifier','input_boolean'), None),
    'brightness': ('turn_on', ('light',), 'brightness_pct'),
    'fan_speed': ('set_percentage', ('fan',), 'percentage'),
    'open_cover': ('open_cover', ('cover',), None),
    'close_cover': ('close_cover', ('cover',), None),
    'cover_position': ('set_cover_position', ('cover',), 'position'),
    'temperature': ('set_temperature', ('climate',), 'temperature'),
    'media_play': ('media_play', ('media_player',), None),
    'media_pause': ('media_pause', ('media_player',), None),
    'number_value': ('set_value', ('number',), 'value'),
    'select_option': ('select_option', ('select',), 'option'),
    'tv_command': ('send_command', ('remote',), 'command'),
    'tv_on': ('turn_on', ('remote',), None),
    'tv_off': ('turn_off', ('remote',), None),
    'scene': ('turn_on', ('scene',), None),
    'script': ('turn_on', ('script',), None),
    'stop_cover': ('stop_cover', ('cover',), None),
    'humidity': ('set_humidity', ('humidifier',), 'humidity'),
    'humidifier_mode': ('set_mode', ('humidifier',), 'mode'),
    'fan_preset': ('set_preset_mode', ('fan',), 'preset_mode'),
    'fan_oscillate': ('oscillate', ('fan',), 'oscillating'),
    'media_play_pause': ('media_play_pause', ('media_player',), None),
    'media_stop': ('media_stop', ('media_player',), None),
    'media_next': ('media_next_track', ('media_player',), None),
    'media_previous': ('media_previous_track', ('media_player',), None),
    'media_volume': ('volume_set', ('media_player',), 'volume_level'),
    'media_mute': ('volume_mute', ('media_player',), 'is_volume_muted'),
    'media_source': ('select_source', ('media_player',), 'source'),
    'media_sound_mode': ('select_sound_mode', ('media_player',), 'sound_mode'),
}

ACTION_OPTIONS={'select_option':'options','fan_preset':'preset_modes','humidifier_mode':'available_modes',
    'media_source':'source_list','media_sound_mode':'sound_mode_list'}
ACTION_BOOLEANS={'fan_oscillate','media_mute'}
RESTORE_DOMAINS={'light','switch','input_boolean','cover','fan','humidifier','climate','number','select'}
RESTORE_ACTIONS={'turn_on','turn_off','toggle','brightness','open_cover','close_cover','cover_position','temperature','humidity','humidifier_mode','fan_speed','fan_preset','fan_oscillate','number_value','select_option','scene','delay'}

def button_restore_targets(actions,entities):
    targets=[]
    for action in actions:
        if action.get('action') not in RESTORE_ACTIONS:raise ValueError('Previous-state restore cannot undo scripts, TV commands or media playback. Remove those steps or turn off restore.')
        for eid in action.get('entities',[]):
            members=entities.get(eid,{}).get('attributes',{}).get('entity_id',[]) if action['action']=='scene' else [eid]
            if isinstance(members,str):members=[members]
            if not isinstance(members,list) or not members:raise ValueError('HA does not report the devices in this preset, so its previous state cannot be captured.')
            for target in members:
                if not isinstance(target,str) or not re.fullmatch(r'[a-z_]+\.[a-z0-9_]+',target) or target.split('.')[0] not in RESTORE_DOMAINS or target not in entities or entities[target].get('disabled_by'):
                    raise ValueError('A selected action contains a device that cannot reliably restore its previous state.')
                if target not in targets:targets.append(target)
    if not targets or len(targets)>100:raise ValueError('Previous-state restore needs between one and 100 supported devices.')
    return targets

def button_restore_config(config,apply_trigger,restore_trigger,actions,targets,key):
    result=copy.deepcopy(config)
    result.pop('trigger',None);result.pop('action',None)
    result.update(mode='restart',triggers=[{**apply_trigger,'id':'apply'},{**restore_trigger,'id':'restore'}])
    result.pop('max',None);result.pop('max_exceeded',None)
    result['actions']=[{'choose':[
        {'conditions':[{'condition':'trigger','id':'apply'}],'sequence':[
            {'action':'mungoon_button_restore.capture','data':{'key':key,'entity_id':targets}},*actions]},
        {'conditions':[{'condition':'trigger','id':'restore'}],
         'sequence':[{'action':'mungoon_button_restore.restore','data':{'key':key}}]}
    ]}]
    return result
# HA entity feature flags; only offer features actually reported by each entity.
ACTION_FEATURES={'fan_speed':1,'fan_oscillate':2,'fan_preset':8,'cover_position':4,'stop_cover':8,
    'media_pause':1,'media_play':16384,'media_play_pause':1,'media_stop':4096,'media_next':32,
    'media_previous':16,'media_volume':4,'media_mute':8,'media_source':2048,'media_sound_mode':65536}

def automation_action_supported(name, entity, services=None):
    if name not in AUTOMATION_ACTIONS or entity.get('disabled_by'):return False
    verb,domains,_=AUTOMATION_ACTIONS[name];domain=entity.get('entity_id','').split('.')[0]
    if domain not in domains or services is not None and verb not in services.get(domain,{}):return False
    if name.startswith('tv_') and entity.get('platform')!='androidtv_remote':return False
    attrs=entity.get('attributes',{});features=attrs.get('supported_features')
    if name in ACTION_FEATURES and isinstance(features,int) and not features&ACTION_FEATURES[name]:return False
    if name in ACTION_OPTIONS and not attrs.get(ACTION_OPTIONS[name]):return False
    return True

def action_value_valid(name, ids, value, entities):
    if name in ACTION_OPTIONS:return isinstance(value,str) and all(value in entities.get(e,{}).get('attributes',{}).get(ACTION_OPTIONS[name],[]) for e in ids)
    if name in ACTION_BOOLEANS:return isinstance(value,bool)
    if name=='humidity':
        return isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value) and value==int(value) and all(entities.get(e,{}).get('attributes',{}).get('min_humidity',0)<=value<=entities.get(e,{}).get('attributes',{}).get('max_humidity',100) for e in ids)
    return True

TV_COMMANDS = ('DPAD_UP','DPAD_DOWN','DPAD_LEFT','DPAD_RIGHT','DPAD_CENTER',
    'HOME','BACK','MENU','VOLUME_UP','VOLUME_DOWN','VOLUME_MUTE',
    'MEDIA_PLAY_PAUSE','MEDIA_PLAY','MEDIA_PAUSE','MEDIA_STOP','MEDIA_NEXT',
    'MEDIA_PREVIOUS','MEDIA_REWIND','MEDIA_FAST_FORWARD')

def tv_action_valid(name, ids, value, entities):
    return (name not in ('tv_command','tv_on','tv_off') or
        (all(entities.get(e,{}).get('platform')=='androidtv_remote' for e in ids)
         and (name!='tv_command' or isinstance(value,str) and value in TV_COMMANDS)))

def setting_action_valid(name, ids, value, entities):
    if name not in ('number_value','select_option'):return True
    import math
    for eid in ids:
        attrs=entities.get(eid,{}).get('attributes',{})
        if name=='select_option':
            if not isinstance(value,str) or value not in attrs.get('options',[]):return False
        else:
            if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value):return False
            low,high,step=attrs.get('min'),attrs.get('max'),attrs.get('step')
            if not all(isinstance(n,(int,float)) and not isinstance(n,bool) and math.isfinite(n) for n in (low,high,step)) or step<=0:return False
            if not low<=value<=high or not math.isclose((value-low)/step,round((value-low)/step),abs_tol=1e-7):return False
    return True

def automation_hash(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode()).hexdigest()

def button_trigger(value):
    value=copy.deepcopy(value)
    if 'platform' in value:value['trigger']=value.pop('platform')
    for key in ('metadata','id','alias','enabled'):value.pop(key,None)
    return value

def is_button_press(trigger):
    """The same physical-press predicate drives discovery and library grouping."""
    return (isinstance(trigger,dict) and bool(trigger.get('device_id'))
        and (trigger.get('trigger') or trigger.get('platform') or 'device')=='device'
        and trigger.get('domain')!='button'
        and bool(re.search(r'(?:^|_)(?:press|pressed|click|clicked|hold|held|release|released)(?:_|$)',str(trigger.get('type','')).lower())))

def is_button_assignment(config):
    if not isinstance(config,dict):return False
    triggers=config.get('triggers',config.get('trigger',[]))
    if isinstance(triggers,dict):triggers=[triggers]
    return isinstance(triggers,list) and any(is_button_press(t) for t in triggers)

def button_label(trigger):
    kind=str(trigger.get('type','')).removeprefix('remote_button_').removeprefix('button_')
    labels={'short_press':'Single press','single_press':'Single press','double_press':'Double press','triple_press':'Triple press','long_press':'Hold','long_release':'Release after hold','short_release':'Release','held':'Hold'}
    return labels.get(kind,kind.replace('_',' ').capitalize())

def button_actions(config,entities=None):
    """Only expose losslessly understood action lists to the simple editor."""
    triggers=config.get('triggers',config.get('trigger',[]))
    if not isinstance(triggers,list) or len(triggers)!=1 or triggers[0].get('enabled',True) is not True or config.get('conditions',config.get('condition',[])) or config.get('use_blueprint'):return None
    actions=config.get('actions',config.get('action',[]));result=[]
    if not isinstance(actions,list) or not actions:return None
    for a in actions:
        if isinstance(a,dict) and set(a)=={'delay'} and isinstance(a['delay'],dict) and set(a['delay'])=={'seconds'}:
            seconds=a['delay']['seconds']
            if isinstance(seconds,(int,float)) and not isinstance(seconds,bool) and 1<=seconds<=86400:
                result.append({'action':'delay','seconds':seconds});continue
            return None
        if not isinstance(a,dict) or set(a)-{'action','service','target','data','metadata'} or a.get('metadata'):return None
        target=a.get('target',{});data=a.get('data',{});service=a.get('action',a.get('service',''))
        if not isinstance(target,dict) or not isinstance(data,dict):return None
        if set(target)=={'device_id'}:
            devices=target['device_id'];devices=[devices] if isinstance(devices,str) else devices
            if not isinstance(devices,list) or not devices or not entities:return None
            ids=sorted(eid for eid,e in entities.items() if e.get('device_id') in devices and not e.get('disabled_by') and eid.split('.')[0]==service.split('.')[0])
            if any(not any(e.get('device_id')==d and eid in ids for eid,e in entities.items()) for d in devices):return None
        elif set(target)=={'entity_id'}:
            ids=target['entity_id'];ids=[ids] if isinstance(ids,str) else ids
        else:return None
        if not isinstance(ids,list) or not ids or any(not isinstance(e,str) or '.' not in e for e in ids):return None
        match=None
        for name,(verb,domains,param) in AUTOMATION_ACTIONS.items():
            if all(e.split('.')[0] in domains and service==e.split('.')[0]+'.'+verb for e in ids) and set(data)==({param} if param else set()):
                if not tv_action_valid(name,ids,data.get(param),entities or {}):continue
                if not setting_action_valid(name,ids,data.get(param),entities or {}):continue
                if not action_value_valid(name,ids,data.get(param),entities or {}):continue
                match={'action':name,'entities':ids}
                if param:match['value']=data[param]*100 if name=='media_volume' and isinstance(data[param],(int,float)) and not isinstance(data[param],bool) else data[param]
                break
        if match is None:return None
        result.append(match)
    return result

def button_assignments(catalog,trigger):
    key=button_trigger(trigger);rows=[]
    for row in catalog['rules']:
        config=row.get('config') or row['rule'].get('config',{})
        triggers=config.get('triggers',config.get('trigger',[]))
        if isinstance(triggers,dict):triggers=[triggers]
        if any(isinstance(t,dict) and button_trigger(t)==key for t in triggers):rows.append(row)
    return rows

def button_revision(rows):
    return automation_hash(sorted([(r['rule']['id'],r.get('remote_revision'),r['rule']['enabled'],r.get('state')) for r in rows]))

def automation_config_hash(value):
    """HA migrates legacy key spellings during validation; compare their meaning.

    Preserve every other field so edits in HA still cause a conflict. In
    particular, never drop unknown actions, conditions, targets or values.
    """
    value=copy.deepcopy(value)
    def sequence(items):
        if isinstance(items,dict):items=[items]
        if not isinstance(items,list):return
        for action in items:
            if not isinstance(action,dict):continue
            if 'service' in action and 'action' not in action:action['action']=action.pop('service')
            for key in ('sequence','then','else','default','parallel'):sequence(action.get(key))
            if isinstance(action.get('repeat'),dict):sequence(action['repeat'].get('sequence'))
            if isinstance(action.get('choose'),list):
                for branch in action['choose']:
                    if isinstance(branch,dict):sequence(branch.get('sequence'))
    if isinstance(value,dict):
        for old,new in (('trigger','triggers'),('condition','conditions'),('action','actions')):
            if old in value and new not in value:value[new]=value.pop(old)
        for trigger in value.get('triggers',[]) if isinstance(value.get('triggers'),list) else []:
            if isinstance(trigger,dict) and 'platform' in trigger and 'trigger' not in trigger:trigger['trigger']=trigger.pop('platform')
        sequence(value.get('actions'))
    return automation_hash(value)

def automation_text(value, label, maximum=100):
    if not isinstance(value,str) or not value.strip() or len(value)>maximum or '{{' in value or '{%' in value:
        raise ValueError('Enter a valid '+label+'.')
    return value.strip()

def automation_number(value, label, low=-100000, high=100000):
    import math
    if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or not low<=value<=high:
        raise ValueError('Enter a valid '+label+'.')
    return value

def automation_time(value):
    if not isinstance(value,str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',value):
        raise ValueError('Choose a valid time.')
    return value+':00'

def compile_automation(rule, entities, services):
    """Validate the complete rule before a NAS or HA write."""
    if not isinstance(rule,dict): raise ValueError('Invalid automation.')
    rid=rule.get('id')
    if not isinstance(rid,str) or not re.fullmatch(r'mungoon_fp_[a-f0-9]{32}',rid): raise ValueError('Invalid automation ID.')
    name=automation_text(rule.get('name'),'automation name')
    if not isinstance(rule.get('enabled'),bool): raise ValueError('Choose whether the automation is enabled.')
    def entity(value):
        if not isinstance(value,str) or not re.fullmatch(r'[a-z_]+\.[a-z0-9_]+',value) or value not in entities or entities[value].get('disabled_by'):
            raise ValueError('A selected device or sensor is no longer available. Refresh the device list.')
        return value
    def clause(c,trigger=False):
        if not isinstance(c,dict): raise ValueError('Choose a condition.')
        kind=c.get('kind');out={}
        if kind in ('state','numeric'):
            eid=entity(c.get('entity_id'));out['entity_id']=eid
            if kind=='state':
                state=automation_text(c.get('state'),'device state',80)
                out['to' if trigger else 'state']=state
            else:
                if c.get('operator') not in ('above','below'): raise ValueError('Choose above or below.')
                out[c['operator']]=automation_number(c.get('value'),'threshold')
            out['platform' if trigger else 'condition']='numeric_state' if kind=='numeric' else 'state'
            if trigger:
                seconds=automation_number(c.get('seconds',0),'duration in seconds',0,86400)
                if seconds:out['for']={'seconds':seconds}
        elif kind=='time':
            if trigger:out={'platform':'time','at':automation_time(c.get('at'))}
            else:
                after=automation_time(c.get('after'));before=automation_time(c.get('before'))
                if after==before:raise ValueError('Choose different start and end times.')
                out={'condition':'time','after':after,'before':before}
        elif kind=='sun' and trigger:
            if c.get('event') not in ('sunrise','sunset'):raise ValueError('Choose sunrise or sunset.')
            out={'platform':'sun','event':c['event']}
            offset=automation_number(c.get('offset_minutes',0),'sun offset in minutes',-720,720)
            if offset:out['offset']={'minutes':offset}
        elif kind=='weekday' and not trigger:
            days=c.get('days')
            if not isinstance(days,list) or not days or len(days)>7 or any(d not in ('mon','tue','wed','thu','fri','sat','sun') for d in days) or len(set(days))!=len(days):raise ValueError('Choose at least one valid weekday.')
            out={'condition':'time','weekday':days}
        else:raise ValueError('Unsupported condition.')
        return out
    triggers=rule.get('triggers',[rule.get('trigger')])
    if not isinstance(triggers,list) or not 1<=len(triggers)<=8:raise ValueError('Add one to eight triggers.')
    triggers=[clause(c,True) for c in triggers]
    conditions=rule.get('conditions',[]);actions=rule.get('actions')
    if not isinstance(conditions,list) or len(conditions)>8: raise ValueError('Use at most eight conditions.')
    if not isinstance(actions,list) or not 1<=len(actions)<=16:raise ValueError('Add one to sixteen actions.')
    condition_mode=rule.get('condition_mode','all')
    if condition_mode not in ('all','any'):raise ValueError('Choose all or any conditions.')
    mode=rule.get('mode','single')
    if mode not in ('single','restart','queued','parallel'):raise ValueError('Choose a valid execution mode.')
    description=rule.get('description','')
    if not isinstance(description,str) or len(description)>500 or '{{' in description or '{%' in description:raise ValueError('Use a description of at most 500 characters.')
    compiled=[]
    for a in actions:
        if isinstance(a,dict) and a.get('action')=='delay':
            compiled.append({'delay':{'seconds':automation_number(a.get('seconds'),'delay in seconds',1,86400)}})
            continue
        if not isinstance(a,dict) or a.get('action') not in AUTOMATION_ACTIONS:raise ValueError('Choose an action.')
        service,domains,parameter=AUTOMATION_ACTIONS[a['action']]
        targets=a.get('entities')
        if not isinstance(targets,list) or not 1<=len(targets)<=100 or len(set(targets))!=len(targets):raise ValueError('Select at least one device for each action.')
        grouped={}
        if not tv_action_valid(a['action'],targets,a.get('value'),entities):
            raise ValueError('Choose a paired Android TV Remote and a supported TV command.')
        if not setting_action_valid(a['action'],targets,a.get('value'),entities):
            raise ValueError('Choose an available device option or a level within every selected device’s range and step.')
        if a['action']=='humidity':automation_number(a.get('value'),'target humidity',0,100)
        if not action_value_valid(a['action'],targets,a.get('value'),entities):
            raise ValueError('Choose a supported mode, option or value for every selected device.')
        for value in targets:
            eid=entity(value);domain=eid.split('.')[0]
            if domain not in domains:raise ValueError('The selected action does not support '+eid+'.')
            if service not in services.get(domain,{}):raise ValueError('This action is not available in Home Assistant.')
            if not automation_action_supported(a['action'],entities[eid],services):raise ValueError('The selected device does not support this action. Refresh and choose a supported control.')
            grouped.setdefault(domain,[]).append(eid)
        data={}
        if parameter:
            v=a.get('value') if parameter in ('command','option','value') or a['action'] in ACTION_OPTIONS or a['action'] in ACTION_BOOLEANS else automation_number(a.get('value'),'action value',-50 if parameter=='temperature' else 0,100)
            if a['action']=='media_volume':v=v/100
            if parameter=='temperature':
                for eid in targets:
                    attrs=entities[eid].get('attributes',{})
                    if v<attrs.get('min_temp',-50) or v>attrs.get('max_temp',100):raise ValueError('Temperature is outside the selected thermostat’s range.')
            data[parameter]=v
        for domain,ids in grouped.items():compiled.append({'service':domain+'.'+service,'target':{'entity_id':ids},**({'data':data} if data else {})})
    compiled_conditions=[clause(c) for c in conditions]
    if condition_mode=='any' and compiled_conditions:compiled_conditions=[{'condition':'or','conditions':compiled_conditions}]
    result={'id':rid,'alias':name,'description':description or 'Created with Mungoon Floor Plan Studio.',
            'trigger':triggers,'condition':compiled_conditions,'action':compiled,
            'mode':mode,'initial_state':rule['enabled']}
    if mode in ('queued','parallel'):
        maximum=automation_number(rule.get('max_runs',10),'maximum simultaneous or queued runs',2,20)
        if int(maximum)!=maximum:raise ValueError('Maximum runs must be a whole number.')
        result['max']=maximum
    return result

def native_rule(config, enabled):
    """Keep every HA field, including blueprints, choose branches and templates."""
    return {'id':str(config.get('id','')),'name':config.get('alias') or 'HA automation',
            'enabled':bool(enabled),'format':'ha','config':copy.deepcopy(config)}

def compile_native(rule):
    rid=rule.get('id')
    if not isinstance(rid,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,150}',rid):raise ValueError('Invalid automation ID.')
    if not isinstance(rule.get('enabled'),bool):raise ValueError('Choose whether the automation is enabled.')
    config=rule.get('config')
    if not isinstance(config,dict) or len(ha_json(config))>80000:raise ValueError('Enter a valid HA configuration object (up to 80 KB).')
    if not ('use_blueprint' in config or ('triggers' in config or 'trigger' in config) and ('actions' in config or 'action' in config)):
        raise ValueError('The HA configuration needs triggers and actions, or a blueprint.')
    config=copy.deepcopy(config);config.update(id=rid,alias=automation_text(rule.get('name'),'automation name'),initial_state=rule['enabled'])
    return config

class AutomationStudio:
    def __init__(self,data,ha):
        self.path=data/'ha-automations.json';self.history=data/'history/ha-automations';self.ha=ha;self.lock=threading.RLock()
    def load(self):return ha_read(self.path,{'version':1,'rules':{}})
    def listing(self):
        with self.lock:
            doc=self.load()
            return {'revision':automation_hash(doc),'rules':[{'rule':v['rule'],'state':v['state'],'error':v.get('error',''),'saved_at':v.get('saved_at')} for v in doc['rules'].values() if v['state']!='removed']}
    def catalog(self):
        """A read never imports or rewrites HA or NAS automation configuration."""
        result=self.listing();entities,_,_=self.environment();doc=self.load();rows=[];seen=set()
        for eid,entity in entities.items():
            if not eid.startswith('automation.'):continue
            attrs=entity.get('attributes',{});rid=str(attrs.get('id') or entity.get('unique_id') or '')
            if rid in seen:continue
            if rid:seen.add(rid)
            enabled=entity.get('state')=='on';config=None;error=''
            safe_id=bool(re.fullmatch(r'[A-Za-z0-9_-]{1,150}',rid))
            if safe_id:
                try:config=self.remote(rid)
                except HAError as exc:error=str(exc)
            previous=doc['rules'].get(rid,{});local=previous.get('rule')
            matches=config is not None and previous.get('confirmed') is not None and automation_config_hash(config)==automation_config_hash(previous['confirmed'])
            # A pending Studio edit must remain available for a safe retry.
            if local and previous.get('state') in ('error','pending'):
                row=next((r for r in result['rules'] if r['rule']['id']==rid),None)
                if row:rows.append({**row,'source':'studio','entity_id':eid,'live_enabled':enabled,'last_triggered':attrs.get('last_triggered')});continue
            if config is not None:
                rule=copy.deepcopy(local) if matches else native_rule(config,enabled)
                rule['enabled']=enabled
                rows.append({'rule':rule,'state':'saved','error':'','source':'studio' if matches else 'ha','entity_id':eid,
                    'remote_revision':automation_config_hash(config),'last_triggered':attrs.get('last_triggered'),
                    'read_only':bool(entity.get('disabled_by')),'notice':'Disabled in the HA entity registry. Edit in HA to enable it.' if entity.get('disabled_by') else '',
                    'ha_url':HA_URL+'/config/automation/edit/'+rid,'config':config})
            else:
                rows.append({'rule':{'id':rid or eid,'name':attrs.get('friendly_name') or eid,'enabled':enabled,'format':'ha','config':{}},
                    'state':'saved','error':error,'source':'ha','entity_id':eid,'read_only':True,'last_triggered':attrs.get('last_triggered'),
                    'notice':error or 'HA does not expose an editable configuration for this rule. Open HA to edit its YAML or package.',
                    'ha_url':HA_URL+'/config/automation/dashboard'})
        for row in result['rules']:
            if row['rule']['id'] not in seen:
                rows.append({**row,'source':'studio','notice':'Not currently loaded in HA. Refresh or check HA.'})
        for row in rows:
            row['button_assignment']=is_button_assignment(row.get('config') or row['rule'].get('config'))
        result.update(rules=sorted(rows,key=lambda r:r['rule']['name'].casefold()),read_at=time.time())
        return result
    def remote(self,rid,method='GET',body=None):
        if not isinstance(rid,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,150}',rid):raise ValueError('Invalid automation ID.')
        req=urllib.request.Request(HA_URL+'/api/config/automation/config/'+rid,data=ha_json(body) if body is not None else None,
            headers={'Authorization':'Bearer '+self.ha.token(),'Content-Type':'application/json'},method=method)
        try:
            with urllib.request.urlopen(req,timeout=20) as response:
                import json
                return json.load(response)
        except urllib.error.HTTPError as error:
            if method=='GET' and error.code==404:return None
            if error.code==400:raise HAError('Home Assistant rejected this automation. Check its devices and values.') from None
            raise HAError('Home Assistant could not save the automation. Reconnect HA or retry.') from None
        except (OSError,ValueError):raise HAError('Home Assistant did not confirm the save. The rule is kept on NAS; retry safely.') from None
    def environment(self):
        with self.ha.websocket(self.ha.token()) as ws:
            states=ws.call('get_states');registry=ws.call('config/entity_registry/list');services=ws.call('get_services');config=ws.call('get_config')
            devices={d['id']:d for d in ws.call('config/device_registry/list')};areas={a['area_id']:a['name'] for a in ws.call('config/area_registry/list')}
        entities={e['entity_id']:e for e in states}
        for e in registry:
            eid=e['entity_id']
            if eid.startswith('automation.') and eid not in entities:entities[eid]={'entity_id':eid,'state':'unavailable','attributes':{'friendly_name':e.get('name') or e.get('original_name') or eid}}
            if eid in entities:
                d=devices.get(e.get('device_id'),{});entities[eid].update(disabled_by=e.get('disabled_by') or d.get('disabled_by'),platform=e.get('platform'),unique_id=e.get('unique_id'),device_id=e.get('device_id'),entity_category=e.get('entity_category'),device_name=d.get('name_by_user') or d.get('name') or '',area_name=areas.get(e.get('area_id') or d.get('area_id'),''))
        return entities,services,config.get('time_zone','Home Assistant local time')
    def options(self):
        entities,services,timezone=self.environment()
        return {'timezone':timezone,'actions':[name for name,(service,domains,_) in AUTOMATION_ACTIONS.items() if name.startswith('tv_') or any(service in services.get(d,{}) for d in domains)],
                'entities':[{'entity_id':eid,'name':e.get('attributes',{}).get('friendly_name',eid),'state':e.get('state','unknown'),
                    'unit':e.get('attributes',{}).get('unit_of_measurement',''),'device_class':e.get('attributes',{}).get('device_class',''),
                    'device_name':e.get('device_name',''),'device_id':e.get('device_id'),'platform':e.get('platform',''),'entity_category':e.get('entity_category'),'area_name':e.get('area_name',''),
                    'supported_actions':[name for name in AUTOMATION_ACTIONS if automation_action_supported(name,e,services)],
                    **({'attributes':{k:v for k,v in e.get('attributes',{}).items() if k in ('min','max','step','options','id','supported_features','min_humidity','max_humidity','available_modes','preset_modes','source_list','sound_mode_list','entity_id')}} if eid.startswith(('number.','select.','humidifier.','fan.','media_player.','scene.','cover.')) else {})}
                    for eid,e in entities.items() if not e.get('disabled_by') and eid.split('.')[0] not in ('automation','update','tts','stt','conversation') and len(eid)<=150]}
    def save(self,body):
        if not isinstance(body,dict):raise ValueError('Invalid automation request.')
        with self.lock:
            doc=self.load()
            if body.get('revision')!=automation_hash(doc):raise AutomationConflict('Automations changed in another session. Reload the list before saving.')
            operation=body.get('operation','save');rule=body.get('rule')
            if operation=='save':
                entities,services,_=self.environment();config=compile_native(rule) if rule.get('format')=='ha' else compile_automation(rule,entities,services);rid=rule['id']
            elif operation in ('retry','remove'):
                rid=body.get('id');record=doc['rules'].get(rid)
                if not record or record['state']=='removed':raise ValueError('Automation not found.')
                if operation=='retry' and record.get('operation')=='remove':operation='remove'
                rule=record['rule'];entities,services,_=self.environment()
                config=(compile_native(rule) if rule.get('format')=='ha' else compile_automation(rule,entities,services)) if operation=='retry' else None
            else:raise ValueError('Unsupported automation operation.')
            old=doc['rules'].get(rid);remote=self.remote(rid)
            expected=body.get('remote_revision')
            if expected is not None and (remote is None or expected!=automation_config_hash(remote)):
                raise AutomationConflict('This automation changed in HA. Refresh it before saving; your draft is preserved.')
            known=[v for v in ((old or {}).get('confirmed'),(old or {}).get('desired')) if v is not None]
            adopting=operation=='save' and rule.get('format')=='ha' and expected is not None and remote is not None
            if not old and not re.fullmatch(r'mungoon_fp_[a-f0-9]{32}',rid) and not adopting:raise ValueError('Refresh the existing HA automation before editing it.')
            if remote is not None and not adopting and not any(automation_config_hash(remote)==automation_config_hash(v) for v in known):
                raise AutomationConflict('This automation was edited in HA. Keep those changes and create a new rule here, or edit it in HA.')
            if old and old.get('confirmed') and remote is None and operation!='remove':
                raise AutomationConflict('This automation was removed in HA. Create a new rule to restore it.')
            if not old and len(doc['rules'])>=200:raise ValueError('Automation library limit reached.')
            # Journal desired bytes before the network mutation. A lost response can
            # be retried against the same ID and cannot duplicate a rule.
            if old:ha_atomic(self.history/(rid+'-'+str(time.time_ns())+'.json'),old)
            if adopting:ha_atomic(self.history/(rid+'-import-'+str(time.time_ns())+'.json'),{'original_ha_config':remote})
            record={'rule':copy.deepcopy(rule),'operation':operation,'state':'pending','desired':config,'confirmed':remote,'error':''}
            doc['rules'][rid]=record;ha_atomic(self.path,doc)
            try:
                if operation=='remove':
                    if remote is not None:self.remote(rid,'DELETE')
                    if self.remote(rid) is not None:raise HAError('HA has not confirmed removal. Retry from the automation list.')
                    record.update(state='removed',confirmed=None,desired=None)
                else:
                    if automation_config_hash(remote)!=automation_config_hash(config):self.remote(rid,'POST',config)
                    actual=self.remote(rid)
                    if automation_config_hash(actual)!=automation_config_hash(config):raise HAError('HA has not confirmed the saved rule. Retry from the automation list.')
                    record.update(state='saved',confirmed=actual,saved_at=time.time())
            except HAError as error:record.update(state='error',error=str(error))
            ha_atomic(self.path,doc)
            return self.listing()

    def button_devices(self,device_id=None):
        devices=[];errors=[]
        with self.ha.websocket(self.ha.token()) as ws:
            registry=ws.call('config/device_registry/list');areas={a['area_id']:a['name'] for a in ws.call('config/area_registry/list')}
            for device in registry:
                if device.get('disabled_by') or device.get('entry_type')=='service' or device_id and device['id']!=device_id:continue
                name=device.get('name_by_user') or device.get('name') or device.get('model') or 'Unnamed button'
                try:triggers=ws.call('device_automation/trigger/list',device_id=device['id']) or []
                except HAError:
                    errors.append(name);continue
                presses={}
                for trigger in triggers:
                    if trigger.get('device_id')!=device['id'] or not is_button_press(trigger):continue
                    clean=button_trigger(trigger);key=automation_hash(clean)
                    presses[key]={'key':key,'label':button_label(clean),'channel':str(clean.get('subtype') or 'button').replace('_',' ').capitalize(),'trigger':clean}
                if presses:devices.append({'id':device['id'],'name':name,'area':areas.get(device.get('area_id'),''),'model':device.get('model') or '', 'presses':sorted(presses.values(),key=lambda p:(p['channel'],{'Single press':0,'Double press':1,'Triple press':2,'Hold':3,'Release':4,'Release after hold':5}.get(p['label'],6),p['label']))})
        return {'devices':sorted(devices,key=lambda d:d['name'].casefold()),'discovery_errors':errors}

    def restore_metadata(self,row):
        record=self.load()['rules'].get(row['rule']['id'],{})
        meta=record.get('rule',{}).get('button_restore')
        return meta if meta and row.get('config') and automation_config_hash(row['config'])==automation_config_hash(record.get('confirmed')) else None

    def buttons(self):
        result=self.button_devices();catalog=self.catalog();entities,_,_=self.environment()
        for device in result['devices']:
            for press in device['presses']:
                rows=button_assignments(catalog,press['trigger']);press['revision']=button_revision(rows);press['assignments']=[]
                for row in rows:
                    meta=self.restore_metadata(row);editable=row.get('config') and not row.get('read_only') and row.get('state')=='saved'
                    assignment={'id':row['rule']['id'],'name':row['rule']['name'],'enabled':row['rule']['enabled'],'ha_url':row.get('ha_url'),
                        'actions':copy.deepcopy(meta['actions']) if meta and editable else button_actions(row['config'],entities) if editable else None}
                    if meta:assignment.update(restore_previous=True,restore_for=meta['apply_key'] if press['key']!=meta['apply_key'] else '',restore_targets=meta['targets'])
                    press['assignments'].append(assignment)
                if press['label']=='Single press':
                    double=next((p for p in device['presses'] if p['channel']==press['channel'] and p['label']=='Double press'),None)
                    if double:
                        paired=button_assignments(catalog,double['trigger'])
                        owned=all((self.restore_metadata(r) or {}).get('apply_key')==press['key'] for r in paired)
                        press['restore']={'key':double['key'],'revision':button_revision(paired),'available':owned,'reason':'' if owned else 'Double press already has an assignment. It will not be overwritten.'}
        result['read_at']=time.time();result['unreadable_rules']=sum(1 for r in catalog['rules'] if not r.get('config'))
        return result

    def save_button(self,body):
        if not isinstance(body,dict) or not isinstance(body.get('device_id'),str) or not isinstance(body.get('press_key'),str):raise ValueError('Choose a button and press.')
        with self.lock:
            devices=self.button_devices(body['device_id'])['devices']
            device=next((d for d in devices if d['id']==body['device_id']),None)
            press=next((p for p in device['presses'] if p['key']==body['press_key']),None) if device else None
            if not press:raise ValueError('HA no longer reports that button press. Refresh Buttons.')
            catalog=self.catalog();rows=button_assignments(catalog,press['trigger']);entities,services,_=self.environment()
            if body.get('revision')!=button_revision(rows):raise AutomationConflict('This press assignment changed in HA. Refresh Buttons before saving; your draft is preserved.')
            restore_meta=self.restore_metadata(rows[0]) if len(rows)==1 else None
            if restore_meta and restore_meta['apply_key']!=press['key']:raise ValueError('Edit the single-press sequence to change this paired restore.')
            if len(rows)>1 or rows and (rows[0].get('read_only') or rows[0].get('state')!='saved' or not restore_meta and button_actions(rows[0].get('config',{}),entities) is None):raise ValueError('This press uses an advanced assignment. Open its advanced editor in Buttons to preserve its logic.')
            rid=rows[0]['rule']['id'] if rows else 'mungoon_fp_'+press['key'][:32]
            name=rows[0]['rule']['name'] if rows else (device['name']+' · '+press['channel']+' · '+press['label'])[:100]
            entities,services,_=self.environment()
            # Reuse the validated action compiler, then use only the discovered trigger.
            compiled=compile_automation({'id':'mungoon_fp_'+press['key'][:32],'name':name,'enabled':body.get('enabled'),'triggers':[{'kind':'time','at':'00:00'}],'conditions':[],'actions':body.get('actions')},entities,services)
            restore=body.get('restore_previous',False)
            if not isinstance(restore,bool):raise ValueError('Choose whether double press restores the previous state.')
            config=copy.deepcopy(rows[0]['config']) if rows else {'id':rid,'alias':name,'triggers':[press['trigger']],'conditions':[],'mode':'single'}
            if restore_meta:
                config.pop('trigger',None);config['triggers']=[press['trigger']];config['mode']=restore_meta.get('previous_mode','single')
                config.update(copy.deepcopy(restore_meta.get('previous_execution',{})))
            if not rows or body['actions']!=button_actions(rows[0]['config'],entities):
                config.pop('action',None);config['actions']=compiled['action']
            config['initial_state']=body['enabled']
            metadata=None
            if restore:
                double=next((p for p in device['presses'] if p['channel']==press['channel'] and p['label']=='Double press'),None)
                if press['label']!='Single press' or not double:raise ValueError('Restore needs single and double press on the same button.')
                paired=button_assignments(catalog,double['trigger'])
                if body.get('restore_revision')!=button_revision(paired):raise AutomationConflict('The double-press assignment changed. Refresh before pairing restore.')
                if any(r['rule']['id']!=rid or (self.restore_metadata(r) or {}).get('apply_key')!=press['key'] for r in paired):raise AutomationConflict('Double press already has an assignment. It was not overwritten.')
                if any(verb not in services.get('mungoon_button_restore',{}) for verb in ('capture','restore')):raise ValueError('The Button Restore integration must be available in HA before enabling previous-state restore.')
                targets=button_restore_targets(body['actions'],entities)
                metadata={'apply_key':press['key'],'restore_key':double['key'],'actions':copy.deepcopy(body['actions']),'targets':targets,'previous_mode':(restore_meta or {}).get('previous_mode',config.get('mode','single'))}
                metadata['previous_execution']={k:copy.deepcopy(config[k]) for k in ('max','max_exceeded') if k in config}
                config=button_restore_config(config,press['trigger'],double['trigger'],compiled['action'],targets,press['key'])
            request={'revision':catalog['revision'],'rule':native_rule(config,body['enabled'])}
            if metadata:request['rule']['button_restore']=metadata
            if rows:request['remote_revision']=rows[0]['remote_revision']
            result=self.save(request);record=next(r for r in result['rules'] if r['rule']['id']==rid)
            return {'id':rid,'state':record['state'],'error':record.get('error','')}
