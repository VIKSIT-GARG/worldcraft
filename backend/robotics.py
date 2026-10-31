"""Simulation-only route bridge. Editor scene setup is never motion evidence."""
import asyncio
import copy
import hashlib
import json
import math
import os
import threading
import time

from pydantic import BaseModel, ConfigDict, Field

from .engine import Engine
from .cyberwave_probe import read_sample


class Mapping(BaseModel):
    model_config=ConfigDict(extra='forbid',allow_inf_nan=False)
    scale:float=Field(default=.05,ge=.04,le=.2)
    origin_x:float=Field(default=20,ge=-1000,le=1000)
    origin_y:float=Field(default=20,ge=-1000,le=1000)
    robot_z:float=Field(default=.35,ge=0,le=2)

    def point(self,p):
        return [round(self.origin_x+(p[0]+240)*self.scale,6),
                round(self.origin_y+p[1]*self.scale,6),self.robot_z]


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()[:24]


def primitive_model(obj,*,patch=False,revision=None):
    from cyberwave.rest.models import (EnvironmentProceduralPrimitiveCreateSchema,
        EnvironmentProceduralPrimitivePatchSchema,Position)
    fields={k:obj[k] for k in ('name','parameters','metadata','fixed_base','visible')}
    fields['position']=Position(actual_instance=obj['position'])
    if patch:
        return EnvironmentProceduralPrimitivePatchSchema(**fields,expected_revision=revision)
    return EnvironmentProceduralPrimitiveCreateSchema(**fields,id=obj['id'],template_key=obj['template_key'])


def scene_objects(scenario,mapping):
    objects=[]
    def box(key,name,x,y,z,width,depth,height,color,role,collision=True):
        objects.append({'id':'worldcraft-'+key,'template_key':'cyberwave/box','name':'WORLDCRAFT / '+name,
            'position':{'x':x,'y':y,'z':z},'fixed_base':True,'visible':True,
            'parameters':{'width':width,'depth':depth,'height':height,'color':color,'disable_collision':not collision},
            'metadata':{'owner':'worldcraft','role':role}})
    scale=mapping.scale
    for b in scenario['world']['buildings']:
        # Decorative city facades overlap vertical roads. Give the robot scene
        # clear lanes while retaining the same entrance/node and road topology.
        node=scenario['world']['nodes'][b['node']]
        p=mapping.point([node[0]+45,node[1]-45]);height=b['height']*scale
        box('building-'+b['id'],b['label'],p[0],p[1],height/2,
            (48 if b['kind']=='hospital' else 35)*scale,36*scale,height,'#91ab95','building')
    for index,r in enumerate(scenario['world']['roads']):
        a,b=[scenario['world']['nodes'][r[k]] for k in ('a','b')]
        p=mapping.point([(a[0]+b[0])/2,(a[1]+b[1])/2]);horizontal=a[1]==b[1]
        width=(23 if r['kind']=='main' else 18)*scale
        closed=r['id'] in scenario['blocked'] or r['kind'] in scenario['blocked']
        box(f'road-{index}',r['id'],p[0],p[1],.005,
            120*scale if horizontal else width,width if horizontal else 120*scale,.01,
            '#ae7058' if closed else '#6b8674','road',False)
        if closed:
            box(f'barrier-{index}','Closed '+r['id'],p[0],p[1],.25,
                .15 if horizontal else width,width if horizontal else .15,.5,'#e58252','barrier')
    return objects


def build_plan(scenario,ambulance,*,mapping=None):
    mapping=Mapping.model_validate(mapping) if mapping is not None else Mapping(**{field:os.environ[env] for field,env in
        [('scale','CYBERWAVE_CITY_SCALE'),('origin_x','CYBERWAVE_CITY_ORIGIN_X'),
         ('origin_y','CYBERWAVE_CITY_ORIGIN_Y'),('robot_z','CYBERWAVE_ROBOT_Z')] if env in os.environ})
    isolated=Engine();sc=copy.deepcopy(scenario);isolated.scenarios={sc['id']:sc}
    vehicle=isolated.vehicle(sc,ambulance)
    if not vehicle['mission']:raise ValueError('Dispatch this ambulance before preparing a robot route.')
    isolated.reset_missions(sc);mission=vehicle['mission']
    if mission['status']=='no_route' or len(mission['points'])<2:
        raise ValueError('No nonzero navigable route for the selected robot mission.')
    objects=scene_objects(sc,mapping)
    environment=os.getenv('CYBERWAVE_ENVIRONMENT_ID','');twin=os.getenv('CYBERWAVE_TWIN_ID','')
    scene_hash=digest({'objects':objects,'mapping':mapping.model_dump(),'environment':environment})
    plan={'scenario_id':sc['id'],'scenario_name':sc['name'],'ambulance':ambulance,'twin_id':twin,
        'environment_id':environment,'mapping':mapping.model_dump(),'mode':'simulation','source_type':'sim_tele',
        'waypoints':[mapping.point(p) for p in mission['points']],'objects':objects,'scene_hash':scene_hash,
        'distance_m':mission['distance'],'robot_distance_m':round(mission['distance']*mapping.scale,6),
        'simulated_time_s':mission['travel_time'],'deadline_seconds':mission.get('deadline_seconds'),
        'assumption':'Miniature robot route, staged at mission origin. Building obstacles are shifted into adjacent blocks for lane clearance; entrances and road graph are preserved. Robot wall-clock time is separate from rescue simulation time.'}
    plan['id']=digest(plan)
    return plan


def execute_route(plan,robot,cancel,publish,*,initial_timeout=8,max_seconds=120):
    """Sequential navigation: each next goal requires a fresh measured arrival."""
    latest=None;guard=threading.Lock();ready=threading.Event()
    state={'status':'checking','verified':False,'command_sent':False,'reached_waypoints':0,'telemetry':None}
    def update(**kw):
        state.update(kw);publish(copy.deepcopy(state))
    def observe(data):
        nonlocal latest
        sample=read_sample(data,time.time())
        if sample:
            with guard:latest=sample
            ready.set()
    def sample():
        with guard:return copy.deepcopy(latest)
    def near(a,b):
        return math.dist(a[:2],b[:2])<=.2 and abs(a[2]-b[2])<=.75
    submitted=False
    try:
        if cancel.is_set():
            update(status='cancelled',detail='Cancelled before dispatch.');return state
        robot.subscribe(observe)
        limit=time.monotonic()+initial_timeout
        while not ready.wait(.01):
            if cancel.is_set():
                update(status='cancelled',detail='Cancelled before dispatch.');return state
            if time.monotonic()>=limit:
                update(status='blocked',detail='No fresh simulator position telemetry. Activate this environment in Cyberwave Playground; no command sent.');return state
        first=sample()
        update(telemetry=first)
        if not near(first['position'],plan['waypoints'][0]):
            update(status='blocked',detail='Robot is not at the mapped mission origin. Stop the simulator and sync/stage this scene first.');return state
        started=time.monotonic();limit=started+max_seconds
        for index,point in enumerate(plan['waypoints'][1:],1):
            if cancel.is_set():
                update(status='cancelled',detail='Stop requested.');return state
            current=sample()
            if time.time()-current['timestamp']>2:
                update(status='blocked',detail='Simulator telemetry became stale; navigation stopped.');return state
            submitted_at=time.time();submitted=True
            response=robot.goto(point)
            update(status='running',command_sent=True,action_id=response.get('action_id'),target=point,
                   detail='Navigation submitted. Waiting for measured waypoint arrival.')
            while True:
                if cancel.is_set():
                    update(status='cancelled',detail='Stop requested.');return state
                current=sample();update(telemetry=current,wall_elapsed_s=round(time.monotonic()-started,2))
                if time.time()-current['timestamp']>2:
                    update(status='blocked',detail='Simulator telemetry became stale; navigation stopped.');return state
                if current['timestamp']>=submitted_at and near(current['position'],point):
                    update(reached_waypoints=index);break
                if time.monotonic()>=limit:
                    update(status='timed_out',detail='Bounded robot run timed out without measured arrival.');return state
                cancel.wait(.1)
        update(status='completed',verified=True,detail='Fresh simulator telemetry reached every mapped waypoint. Rescue deadline metrics remain separate.')
        return state
    except Exception as exc:
        update(status='failed',verified=False,detail=f'Robot navigation failed ({type(exc).__name__}); check simulator readiness, access and controller support.')
        return state
    finally:
        if submitted:
            try:robot.stop();update(stop_sent=True)
            except Exception:update(status='failed',verified=False,stop_sent=False,detail='Stop command failed. Stop the robot in Cyberwave Playground.')


def verify_scene_objects(plan,settings):
    stored={p['id']:p for p in (settings or {}).get('procedural_primitives',[])}
    desired={p['id'] for p in plan['objects']}
    for obj in plan['objects']:
        actual=stored.get(obj['id'],{})
        if actual.get('metadata',{}).get('owner')!='worldcraft':raise ValueError('Robot scene is missing. Sync the scene before execution.')
        for key in ('position','fixed_base','visible'):
            if actual.get(key)!=obj[key]:raise ValueError('Robot scene changed; sync the current plan before execution.')
        if any(actual.get('parameters',{}).get(k)!=v for k,v in obj['parameters'].items()):
            raise ValueError('Robot scene parameters changed; sync the current plan before execution.')
    for key,obj in stored.items():
        if obj.get('metadata',{}).get('owner')=='worldcraft' and key not in desired:
            if obj.get('visible') or not obj.get('parameters',{}).get('disable_collision'):
                raise ValueError('A stale WORLDCRAFT object remains active; sync the scene first.')


class CyberwaveRobot:
    """Official SDK transport, simulation-only. No simulator is auto-started."""
    def __init__(self,plan,*,require_navigation=True):
        from cyberwave import Cyberwave
        if not os.getenv('CYBERWAVE_API_KEY') or not plan.get('environment_id') or not plan.get('twin_id'):
            raise ValueError('Configure CYBERWAVE_API_KEY, CYBERWAVE_ENVIRONMENT_ID and CYBERWAVE_TWIN_ID.')
        self.plan=plan
        self.client=Cyberwave(api_key=os.environ['CYBERWAVE_API_KEY'],mode='playground',
                              environment_id=plan['environment_id'],timeout=8)
        self.client.affect('playground')
        try:
            self.robot=self.client.twins.get(plan['twin_id'])
            raw=self.client.twins.get_raw(plan['twin_id'])
            if str(raw.environment_uuid)!=plan['environment_id']:
                raise ValueError('Configured twin is not in the configured environment.')
            surfaces=self.client.agents.control.list_surfaces(plan['environment_id'],mode='simulation') if require_navigation else []
            surface=next((s for s in surfaces if str(s['twin_uuid'])==plan['twin_id']),{})
            if require_navigation and (not surface.get('runtimes',{}).get('simulation',{}).get('available') or not any(
                c['kind']=='navigation_command' and c['available'] for c in surface.get('controls',[]))):
                raise ValueError('This twin has no available simulation navigation control.')
        except Exception:
            self.close();raise

    def subscribe(self,callback):self.robot.subscribe_position(callback)
    def goto(self,point):
        return self.robot.navigation.goto(point,environment_uuid=self.plan['environment_id'],
            source_type='sim_tele',reference_frame='map',metadata={'worldcraft_plan':self.plan['id']})
    def stop(self):
        return self.robot.navigation.stop(environment_uuid=self.plan['environment_id'],source_type='sim_tele')
    def close(self):self.client.disconnect()

    def scene(self):
        return self.client.environments.get(self.plan['environment_id'])

    def verify_scene(self):
        verify_scene_objects(self.plan,self.scene().settings)

    def sync(self,cancel,publish):
        env=self.plan['environment_id']
        if self.client.environments.simulations.list(env):
            raise ValueError('Stop the active Cyberwave simulation before editing/staging its scene.')
        active=threading.Event()
        self.robot.subscribe_position(lambda data:active.set() if read_sample(data,time.time()) else None)
        if active.wait(1):
            raise ValueError('Fresh simulator telemetry is active. Stop Playground before editing/staging the scene.')
        stored={p['id']:p for p in (self.scene().settings or {}).get('procedural_primitives',[])}
        desired={p['id']:p for p in self.plan['objects']}
        for key,obj in stored.items():
            if obj.get('metadata',{}).get('owner')=='worldcraft' and key not in desired:
                desired[key]={**obj,'visible':False,'parameters':{**obj.get('parameters',{}),'disable_collision':True}}
        for i,(key,obj) in enumerate(desired.items()):
            if cancel.is_set():raise ValueError('Scene setup cancelled; re-sync before running.')
            if key in stored and stored[key].get('metadata',{}).get('owner')!='worldcraft':
                raise ValueError('Scene object ID collision with an unrelated object; no overwrite allowed.')
            if key in stored:
                patch=primitive_model(obj,patch=True,revision=stored[key].get('revision'))
                self.client.api.src_app_api_environments_update_environment_procedural_primitive(env,key,patch)
            else:
                self.client.api.src_app_api_environments_create_environment_procedural_primitive(env,primitive_model(obj))
            publish({'status':'syncing','objects_written':i+1,'objects_total':len(desired),'verified':False})
        # Editor staging is explicit and happens only during stopped scene setup.
        p=self.plan['waypoints'][0]
        self.client.twins.update_state(self.plan['twin_id'],dict(zip(('position_x','position_y','position_z'),p)))
        self.verify_scene()
        return {'status':'scene_ready','scene_hash':self.plan['scene_hash'],'verified':False,
                'detail':'Scene synced and robot staged in the editor. Activate Playground, then execute. No robot movement verified.'}


class RobotBridge:
    def __init__(self):
        self.guard=threading.Lock();self.cancel=threading.Event();self.task=None
        self.prepared=None;self.scene_hash=None;self.result={'status':'idle','verified':False}

    def snapshot(self):
        with self.guard:return copy.deepcopy({'plan':self.prepared,'job':self.result})

    def prepare(self,plan):
        if self.task and not self.task.done():raise ValueError('A robot operation is already active; stop it first.')
        with self.guard:
            self.prepared=copy.deepcopy(plan)
            self.result={'status':'prepared','verified':False,'telemetry':None,'command_sent':False,
                         'detail':'Route prepared. Sync/stage its scene while stopped, then activate Playground and execute.'}
        return self.snapshot()

    def update(self,data):
        with self.guard:self.result={**self.result,**copy.deepcopy(data)}

    async def start(self,plan,kind,mutex):
        if mutex.locked() or (self.task and not self.task.done()):raise ValueError('A Cyberwave operation is already active.')
        if not self.prepared or self.prepared['id']!=plan['id']:raise ValueError('Prepare the current robot route first; the scenario or configuration changed.')
        await mutex.acquire();self.cancel=threading.Event()
        self.update({'status':'connecting','kind':kind,'plan_id':plan['id'],'verified':False,
                     'command_sent':False,'reached_waypoints':0,'telemetry':None,'stop_sent':None,'detail':'Checking Cyberwave simulation control.'})
        def work():
            transport=None
            try:
                transport=CyberwaveRobot(plan)
                if kind=='sync':
                    self.scene_hash=None
                    result=transport.sync(self.cancel,self.update)
                    self.scene_hash=plan['scene_hash']
                else:
                    transport.verify_scene()
                    result=execute_route(plan,transport,self.cancel,self.update)
                self.update(result)
            except Exception as exc:
                detail=str(exc) if isinstance(exc,ValueError) else f'Cyberwave SDK failed ({type(exc).__name__}); check configuration, permissions and simulator.'
                self.update({'status':'blocked','verified':False,'detail':detail})
            finally:
                if transport:
                    try:transport.close()
                    except Exception:pass
        async def run():
            try:await asyncio.to_thread(work)
            finally:mutex.release()
        self.task=asyncio.create_task(run())
        return self.snapshot()

    def stop(self):
        self.cancel.set()
        if self.task and not self.task.done():self.update({'status':'stopping','verified':False,'detail':'Stop requested; awaiting SDK stop acknowledgement.'})
        return self.snapshot()

    async def shutdown(self):
        self.stop()
        if self.task:await self.task
