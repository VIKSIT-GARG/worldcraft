"""Create/import Cyberwave twins and test them using simulator-origin telemetry.

The built-in rover is an authored prototype, not a promise that Cyberwave's
prompt builders can synthesize arbitrary robots. Local physics tests are labeled.
"""
import asyncio
import copy
import json
import math
import os
from pathlib import Path
import tempfile
import threading
import time
import uuid

from .robotics import digest, read_sample

MODEL=Path(__file__).with_name('forward_rover.xml')
DATA=Path(__file__).resolve().parents[1]/'data'/'robot-workshop.json'


def mapping_config():
    from .robotics import Mapping
    return Mapping(origin_x=float(os.getenv('CYBERWAVE_WORKSHOP_ORIGIN_X','20')),
                   origin_y=float(os.getenv('CYBERWAVE_WORKSHOP_ORIGIN_Y','-20'))).model_dump()


def scene_plan(record,scenario):
    from .robotics import Mapping,scene_objects
    mapping=Mapping.model_validate(record['mapping']);objects=scene_objects(scenario,mapping)
    result={'objects':objects,'mapping':mapping.model_dump(),'environment_id':record['environment_id'],
            'twin_id':record['twin_id'],'waypoints':[mapping.point([-240,0])]}
    result['scene_hash']=digest({'objects':objects,'mapping':mapping.model_dump(),'environment':record['environment_id']})
    result['id']=digest(result)
    return result


def forward_controls(speed):
    if isinstance(speed,bool) or not math.isfinite(speed) or not 0<=speed<=.48:
        raise ValueError('Forward speed must be finite and between 0 and 0.48 m/s. Reverse and turning are unsupported.')
    return {'left_drive':speed/.08,'right_drive':speed/.08}


def rover_schema():
    from cyberwave_robot_format import export_universal_schema_json,JointType
    from cyberwave_robot_format.mjcf import MJCFParser
    schema=MJCFParser().parse(MODEL,resolve_inertia=True)
    # The local test floor belongs to the environment, not to each imported robot.
    world=next(link for link in schema.links if link.name=='world')
    world.visuals=[];world.collisions=[]
    for joint in schema.joints:
        if joint.name in ('left_drive','right_drive'):joint.type=JointType.CONTINUOUS
    schema.extensions['cyberwave']={'capabilities':{'has_joints':True,'can_actuate':True,
        'can_locomote':True,'can_fly':False,'has_wheels':True,'locomotion_mode':'wheeled',
        'sensors':[]}}
    return json.loads(export_universal_schema_json(schema))


def validate_rover():
    import mujoco
    import xml.etree.ElementTree as ET
    from cyberwave_robot_format import CommonSchema
    from cyberwave_robot_format.mjcf import MJCFExporter
    # Test the cloud payload after a real exporter round trip, not just its source.
    with tempfile.TemporaryDirectory(dir=MODEL.parent) as directory:
        path=Path(directory)/'robot.xml'
        MJCFExporter().export(CommonSchema.from_dict(rover_schema()),path)
        tree=ET.parse(path)
        ET.SubElement(tree.getroot().find('worldbody'),'geom',{'name':'test_floor','type':'plane','size':'10 10 .1'})
        tree.write(path)
        model=mujoco.MjModel.from_xml_path(str(path))
    state=mujoco.MjData(model)
    for _ in range(2000):state.ctrl[:]=3;mujoco.mj_step(model,state)
    distance=float(state.qpos[0]);lateral=float(state.qpos[1])
    for _ in range(1000):state.ctrl[:]=0;mujoco.mj_step(model,state)
    return {'runtime':'local MuJoCo','model':'Cyberwave schema round trip','version':mujoco.__version__,'forward_distance_m':distance,
            'lateral_distance_m':lateral,'stopped_speed_m_s':float(math.hypot(*state.qvel[:2])),
            'cloud_verified':False,'assumptions':'Authored 1 kg chassis, 8 cm wheels, synthetic friction and motor parameters; not hardware measurements.'}


def validate_upload(content,filename):
    """First import format: self-contained native schema, no external mesh paths."""
    from cyberwave_robot_format import CommonSchema,export_universal_schema_json
    if len(content)>2*1024*1024 or not filename.lower().endswith('.json'):
        raise ValueError('Upload a self-contained Cyberwave robot-format JSON file under 2 MiB. Mesh/URDF packages use Cyberwave import, then catalog import here.')
    try:
        data=json.loads(content)
        def inspect(node):
            if isinstance(node,dict):
                for key,value in node.items():
                    if key in ('filename','file','uri','url','mesh_path','texture') and value:
                        raise ValueError('External geometry/resources are not accepted by this import path.')
                    inspect(value)
            elif isinstance(node,list):
                for value in node:inspect(value)
        inspect(data)
        schema=CommonSchema.from_dict(data)
        if not schema.links or len(schema.links)>100 or len(schema.joints)>100:
            raise ValueError('A robot must contain 1–100 links and at most 100 joints.')
        return json.loads(export_universal_schema_json(schema))
    except (ValueError,TypeError,KeyError,AttributeError,RecursionError) as exc:
        raise ValueError('Invalid self-contained robot schema. Check geometry, joint limits and physical properties.') from exc


class Cloud:
    def __init__(self):
        from cyberwave import Cyberwave
        if not os.getenv('CYBERWAVE_API_KEY') or not os.getenv('CYBERWAVE_ENVIRONMENT_ID'):
            raise ValueError('Configure CYBERWAVE_API_KEY and CYBERWAVE_ENVIRONMENT_ID.')
        self.environment=os.environ['CYBERWAVE_ENVIRONMENT_ID']
        self.client=Cyberwave(api_key=os.environ['CYBERWAVE_API_KEY'],mode='playground',environment_id=self.environment,timeout=8)
        self.client.affect('playground')

    def close(self):self.client.disconnect()

    def catalog(self,query):
        assets=self.client.assets.search(query)
        return [self.asset_info(a) for a in assets[:20]]

    @staticmethod
    def asset_info(asset):
        return {'id':str(asset.uuid),'name':asset.name,'capabilities':asset.capabilities or {},
                'supported_simulation_backends':getattr(asset,'supported_simulation_backends',None) or [],
                'has_physics':bool(getattr(asset,'has_universal_schema',False))}

    def create(self,schema,name,source):
        environment=self.client.environments.get(self.environment)
        model_hash=digest(schema)
        # Reuse only an exact owned model, never a similarly named public asset.
        candidates=self.client.assets.search(name)
        asset=next((a for a in candidates if str(a.workspace_uuid)==str(environment.workspace_uuid)
            and (a.metadata or {}).get('worldcraft_model_hash')==model_hash),None)
        if asset is None:
            asset=self.client.assets.create(name=name,description='WORLDCRAFT authored/imported prototype. Simulation only.',
                workspace_uuid=str(environment.workspace_uuid),visibility='private',universal_schema=schema,
                metadata={'owner':'worldcraft','worldcraft_model_hash':model_hash,'source':source},
                supported_simulation_backends=['mujoco'],is_purchasable=False,is_rentable=False)
        return asset

    def instantiate(self,asset_id,name,scenario_id,mapping):
        if self.client.environments.simulations.list(self.environment):
            raise ValueError('Stop the Cyberwave simulation before importing a robot into its scene.')
        twin=self.client.twins.create(asset_id=asset_id,environment_id=self.environment,name=name,
            position_x=mapping['origin_x'],position_y=mapping['origin_y'],position_z=0,
            metadata={'worldcraft_scenario':scenario_id,'owner':'worldcraft'})
        raw=self.client.twins.get_raw(str(twin.uuid))
        if str(raw.environment_uuid)!=self.environment:raise ValueError('Imported twin environment mismatch.')
        return str(twin.uuid)

    def preflight(self):
        return self.client.environments.simulations.preflight(self.environment,backend='mujoco')

    def design(self,prompt):
        from cyberwave.rest.api.asset_generations_api import AssetGenerationsApi
        from cyberwave.rest.models import AssetGenerationPlanSchema
        env=self.client.environments.get(self.environment)
        api=AssetGenerationsApi(self.client.api.api_client)
        return api.src_app_api_asset_generations_plan_asset_generation(
            AssetGenerationPlanSchema(prompt=prompt,workspace_uuid=str(env.workspace_uuid)))


def run_forward(robot,profile,speed,duration,cancel,publish,*,initial_timeout=8):
    """SDK simulation controls; position change must be measured, never echoed."""
    forward_controls(speed)
    if not math.isfinite(duration) or not .2<=duration<=10:raise ValueError('Test duration must be 0.2–10 seconds.')
    samples=[];guard=threading.Lock();ready=threading.Event();sent=False
    result={'status':'checking','verified':False,'command_sent':False,'telemetry':None}
    def update(**kw):result.update(kw);publish(copy.deepcopy(result))
    def observe(data):
        sample=read_sample(data,time.time())
        if sample:
            with guard:samples.append(sample);samples[:]=samples[-512:]
            ready.set()
    def latest():
        with guard:return copy.deepcopy(samples[-1]) if samples else None
    def command(value):
        if profile=='forward_only':
            robot.joints.set(forward_controls(value),what_data='velocity',source_type='sim_tele')
        else:
            robot.move_forward(value,duration=.2,source_type='sim_tele')
    try:
        robot.subscribe_position(observe)
        deadline=time.monotonic()+initial_timeout
        while not ready.wait(.1):
            if cancel.is_set():update(status='cancelled');return result
            if time.monotonic()>deadline:
                update(status='blocked',detail='No fresh Cyberwave simulator telemetry. No command sent.');return result
        before=latest();started=time.time()
        if started-before['timestamp']>2:
            update(status='blocked',detail='Simulator telemetry is stale. No command sent.');return result
        deadline=time.monotonic()+duration
        while time.monotonic()<deadline:
            if cancel.is_set():update(status='cancelled',detail='Forward test cancelled.');return result
            current=latest()
            if time.time()-current['timestamp']>2:
                update(status='blocked',detail='Simulator telemetry stopped; stopping robot.');return result
            sent=True;command(speed)
            update(status='running',command_sent=True,telemetry=current)
            cancel.wait(.1)
        command(0)
        after=latest();distance=math.dist(before['position'][:2],after['position'][:2])
        verified=after['timestamp']>=started and distance>.01
        update(status='completed' if verified else 'unverified',verified=verified,telemetry=after,
               distance_m=distance,speed_command_m_s=speed,duration_s=duration,
               detail='Measured Cyberwave simulator displacement; not a local animation.' if verified else 'Commands sent; simulator displacement not verified.')
        return result
    finally:
        if sent:
            try:command(0);update(stop_sent=True)
            except Exception:update(status='failed',verified=False,stop_sent=False,detail='Stop command failed; stop the Cyberwave simulation.')


class Workshop:
    def __init__(self,path=DATA):
        self.path=path;self.guard=threading.Lock();self.cancel=threading.Event();self.task=None
        self.robots={};self.job={'status':'idle','verified':False};self.tests=[];self.simulation=None
        if path.exists():
            try:
                saved=json.loads(path.read_text());self.robots=saved['robots'];self.tests=saved.get('tests',[])[-20:]
            except (ValueError,KeyError):pass

    def save(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        temporary=self.path.with_suffix('.tmp');temporary.write_text(json.dumps({'robots':self.robots,'tests':self.tests[-20:]},indent=2)+'\n')
        temporary.replace(self.path)

    def snapshot(self):
        with self.guard:return copy.deepcopy({'robots':list(self.robots.values()),'job':self.job,
            'tests':self.tests[-20:],'simulation':self.simulation,'active':bool(self.task and not self.task.done())})

    def update(self,changes):
        with self.guard:self.job.update(copy.deepcopy(changes))

    async def launch(self,operation,mutex):
        if mutex.locked() or (self.task and not self.task.done()):raise ValueError('A Cyberwave operation is active; stop it first.')
        await mutex.acquire();self.cancel=threading.Event()
        with self.guard:self.job={'status':'connecting','verified':False,'detail':'Connecting to Cyberwave.'}
        def work():
            cloud=None
            try:
                cloud=Cloud();result=operation(cloud)
                self.update(result)
            except Exception as exc:
                self.update({'status':'blocked','verified':False,'detail':str(exc) if isinstance(exc,ValueError)
                    else f'Cyberwave request failed ({type(exc).__name__}). Check access, simulator readiness and quotas.'})
            finally:
                if cloud:
                    try:cloud.close()
                    except Exception:pass
        async def run():
            try:await asyncio.to_thread(work)
            finally:mutex.release()
        self.task=asyncio.create_task(run())
        return self.snapshot()

    def create(self,cloud,scenario_id,mapping,*,schema=None,asset_id=None,source='forward_only'):
        if len(self.robots)>=8:raise ValueError('Maximum eight workshop robots. Reuse an imported robot.')
        if asset_id:
            asset=cloud.client.assets.get(asset_id);profile='catalog'
        else:
            schema=schema if schema is not None else rover_schema()
            asset=cloud.create(schema,'WORLDCRAFT Forward Rover' if source=='forward_only' else 'WORLDCRAFT Imported Robot',source)
            profile=source
        twin_id=cloud.instantiate(str(asset.uuid),asset.name,scenario_id,mapping)
        record={'id':uuid.uuid4().hex[:12],'scenario_id':scenario_id,'asset_id':str(asset.uuid),
                'twin_id':twin_id,'environment_id':cloud.environment,'name':asset.name,'profile':profile,
                'mapping':mapping,'capabilities':asset.capabilities or {},'verified':False,
                'allowed_actions':['forward','stop','observe'] if profile=='forward_only' else ['observe'],
                'editor_position':[mapping['origin_x'],mapping['origin_y'],0],
                'telemetry':None,'status':'created','detail':'Created in Cyberwave; no simulated movement verified.'}
        with self.guard:self.robots[record['id']]=record;self.save()
        return {'status':'created','verified':False,'robot_id':record['id'],'detail':record['detail']}

    def selected(self,robot_id,scenario_id):
        candidates=[r for r in self.robots.values() if r['scenario_id']==scenario_id]
        record=self.robots.get(robot_id) if robot_id else candidates[-1] if candidates else None
        if not record or record['scenario_id']!=scenario_id:raise ValueError('Select a robot imported into this scenario.')
        return copy.deepcopy(record)

    def test(self,cloud,record,speed,duration,scenario=None):
        if record['profile']!='forward_only':raise ValueError('This import has no reviewed forward-only controller. Observation is available; do not infer controls from its appearance.')
        if scenario is not None:
            from .robotics import verify_scene_objects
            if scenario['id']!=record['scenario_id']:raise ValueError('Robot is attached to another city.')
            verify_scene_objects(scene_plan(record,scenario),cloud.client.environments.get(record['environment_id']).settings)
        runs=cloud.client.environments.simulations.list(record['environment_id'])
        if any(r.status!='running' or r.raw.get('simulation_ready') is False for r in runs):
            return {'status':'blocked','verified':False,'detail':'Cyberwave simulator is loading or explicitly unready. Wait for readiness before sending robot commands.'}
        robot=cloud.client.twins.get(record['twin_id'])
        raw=cloud.client.twins.get_raw(record['twin_id'])
        if str(raw.environment_uuid)!=record['environment_id']:raise ValueError('Robot environment changed.')
        def publish(update):
            self.update(update)
            if update.get('telemetry'):
                with self.guard:self.robots[record['id']]['telemetry']=copy.deepcopy(update['telemetry'])
        result=run_forward(robot,record['profile'],speed,duration,self.cancel,publish)
        row={'robot_id':record['id'],'runtime':'Cyberwave simulation','speed_command_m_s':speed,'duration_s':duration,**result}
        with self.guard:
            self.tests.append(row);self.tests=self.tests[-20:]
            self.robots[record['id']].update(telemetry=result.get('telemetry'),verified=result['verified'],status=result['status'])
            self.save()
        return result

    def observe(self,cloud,record):
        robot=cloud.client.twins.get(record['twin_id']);observed=[];ready=threading.Event()
        def callback(data):
            sample=read_sample(data,time.time())
            if sample:observed.append(sample);ready.set()
        robot.subscribe_position(callback)
        if not ready.wait(8):return {'status':'blocked','verified':False,'detail':'No fresh Cyberwave simulator telemetry.'}
        sample=observed[-1]
        with self.guard:self.robots[record['id']]['telemetry']=sample;self.save()
        return {'status':'observed','verified':False,'telemetry':sample,'detail':'Fresh simulator pose observed. This alone does not verify commanded movement.'}

    def preflight(self,cloud):
        result=cloud.preflight()
        return {'status':'ready' if result.get('eligible') else 'blocked','verified':False,
                'preflight':result,'detail':'Cyberwave MuJoCo eligibility checked. Readiness is not robot movement.'}

    def start_simulation(self,cloud,duration,accept_cost):
        if accept_cost is not True:raise ValueError('Review and accept the Cyberwave simulation cost before starting.')
        if duration not in (30,60,120):raise ValueError('Select a bounded 30, 60 or 120 second simulation.')
        active=cloud.client.environments.simulations.list(cloud.environment)
        if active:raise ValueError('A Cyberwave simulation is already active; observe that run rather than starting another.')
        eligibility=cloud.preflight()
        if not eligibility.get('eligible'):return {'status':'blocked','verified':False,'preflight':eligibility,'detail':'Cyberwave refused simulation eligibility. Resolve its blockers first.'}
        run=cloud.client.environments.simulations.start(cloud.environment,backend='mujoco',duration=duration,stream_data=True)
        self.simulation={'id':run.simulation_id,'status':run.status,'environment_id':cloud.environment,
                         'duration_s':duration,'owned':True,'runtime':'Cyberwave MuJoCo'}
        return {'status':'simulation_requested','verified':False,'detail':'Bounded Cyberwave physics simulation requested. Poll status and require fresh telemetry before testing movement.'}

    def simulation_status(self,cloud):
        runs=cloud.client.environments.simulations.list(cloud.environment)
        summary=[{'id':r.simulation_id,'status':r.status,'backend':r.backend,
                  'simulation_ready':r.raw.get('simulation_ready')} for r in runs]
        if self.simulation:
            current=next((r for r in summary if r['id']==self.simulation['id']),None)
            self.simulation.update(status=current['status'] if current else 'not_active')
        return {'status':'simulation_status','verified':False,'active_simulations':summary,
                'detail':'Cloud lifecycle status only; motion verification requires simulator telemetry.'}

    def stop_simulation(self,cloud):
        if not self.simulation or not self.simulation.get('owned'):raise ValueError('No simulation started by this workshop is selected.')
        if self.simulation['environment_id']!=cloud.environment:raise ValueError('Simulation environment configuration changed.')
        cloud.client.environments.simulations.stop(cloud.environment,self.simulation['id'])
        self.simulation['status']='stop_requested'
        return {'status':'simulation_stop_requested','verified':False,'detail':'Cyberwave accepted the simulation stop request. Refresh lifecycle status.'}

    def sync_scene(self,cloud,record,scenario):
        from .robotics import CyberwaveRobot
        plan=scene_plan(record,scenario)
        transport=CyberwaveRobot(plan,require_navigation=False)
        try:
            result=transport.sync(self.cancel,self.update)
            with self.guard:
                self.robots[record['id']].update(editor_position=plan['waypoints'][0],telemetry=None,verified=False)
                self.save()
            return result
        finally:transport.close()

    def stop(self):
        self.cancel.set()
        if self.task and not self.task.done():self.update({'status':'stopping','verified':False,'detail':'Stopping the active operation.'})
        return self.snapshot()

    async def shutdown(self):
        self.stop()
        if self.task:await self.task
