import asyncio
import copy
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from .engine import Engine, Operation
from . import integrations, explanations, robotics, workshop

load_dotenv(Path(__file__).resolve().parents[1]/'.env')
engine=Engine();lock=asyncio.Lock()
provider_status=integrations.status()
control={'paused':False,'speed':1.}
cyber_lock=asyncio.Lock()
robot_bridge=robotics.RobotBridge()
robot_workshop=workshop.Workshop()

@asynccontextmanager
async def lifespan(app):
    async def clock():
        previous=time.monotonic()
        while True:
            await asyncio.sleep(.05)
            now=time.monotonic();dt=min(now-previous,.25);previous=now
            async with lock:
                if not control['paused']: engine.tick(dt*control['speed'])
    task=asyncio.create_task(clock())
    yield
    await robot_bridge.shutdown()
    await robot_workshop.shutdown()
    task.cancel()
    try: await task
    except asyncio.CancelledError: pass

app=FastAPI(title='WORLDCRAFT',version='0.1.0',lifespan=lifespan)

@app.exception_handler(integrations.IntegrationError)
async def integration_error(request,exc):
    if exc.provider in provider_status: provider_status[exc.provider].update(verified=False,detail=exc.message,request_id=None)
    return JSONResponse(status_code=exc.status,content={'detail':exc.message,'provider':exc.provider})

@app.exception_handler(ValueError)
async def value_error(request,exc): return JSONResponse(status_code=422,content={'detail':str(exc)})

@app.get('/api/health')
async def health(): return {'ok':True,'service':'worldcraft','simulation':'kinematic','storage':'in-memory, single process'}

@app.get('/api/state')
async def state():
    async with lock: return {**engine.snapshot(),'control':dict(control),'integrations':copy.deepcopy(provider_status),'robotics':robot_bridge.snapshot(),'workshop':robot_workshop.snapshot()}

class Command(BaseModel):
    model_config=ConfigDict(extra='forbid')
    text: str=Field(min_length=1,max_length=2000)
    mode: Literal['gemini','local']='gemini'
    scenario_id: str|None=None

@app.post('/api/command')
async def command(req:Command):
    async with lock:
        context=copy.deepcopy({'scenario_id':req.scenario_id,'worlds':[{'id':s['id'],'name':s['name'],'blocked':s['blocked'],'buildings':[b['id'] for b in s['world']['buildings']],'roads':s['world']['roads'],'ambulances':s['ambulances']} for s in engine.scenarios.values()]})
        context['workshop_robots']=[{k:r[k] for k in ('id','scenario_id','name','profile','allowed_actions')} for r in robot_workshop.snapshot()['robots']]
    request_id=None
    if req.mode=='gemini':
        op,request_id=await asyncio.to_thread(integrations.gemini_plan,req.text,context)
        provider_status['gemini'].update(verified=True,detail='Successful Vertex AI Gemini response and validated operation. World execution is checked separately.',request_id=request_id)
    else: op=integrations.local_plan(req.text)
    extras={}
    if op.action in ('robot_create','robot_forward','robot_observe','robot_catalog','robot_design'):
        extras['workshop_result']=await workshop_operation(op,req.scenario_id)
        sid=req.scenario_id
    elif op.action.startswith('robot_'):
        extras['robotics_result']=await robot_operation(op.action,req.scenario_id,op.ambulance)
        sid=req.scenario_id
    elif op.action=='cyberwave_run':
        extras['cyberwave_result']=await cyberwave_probe()
        sid=req.scenario_id
    elif op.action=='explain':
        extras['explanation']=await explain_analysis()
        sid=req.scenario_id
    else:
        async with lock:
            require_robot_idle()
            sid=engine.apply(op,req.scenario_id)
    async with lock:
        return {'scenario_id':sid,'operation':op.model_dump(),'planner':req.mode,'request_id':request_id,**engine.snapshot(),**extras}

class Action(BaseModel):
    operation:Operation
    scenario_id:str|None=None

@app.post('/api/action')
async def action(req:Action):
    async with lock:
        require_robot_idle()
        sid=engine.apply(req.operation,req.scenario_id)
        return {'scenario_id':sid,'planner':'manual',**engine.snapshot()}

@app.post('/api/analysis/explain')
async def explain_analysis():
    async with lock:
        report=copy.deepcopy(engine.analysis)
    if not report: raise HTTPException(422,'Run Break My Plan first.')
    return await asyncio.to_thread(explanations.explain,report)

class Controls(BaseModel):
    paused:bool|None=None
    speed:float|None=Field(default=None,ge=0.25,le=20)

@app.post('/api/control')
async def controls(req:Controls):
    async with lock:
        control.update(req.model_dump(exclude_none=True));return dict(control)

@app.post('/api/scenarios/{sid}/reset')
async def reset(sid:str):
    async with lock:
        require_robot_idle()
        engine.reset_missions(engine.scenario(sid));return engine.snapshot()

def require_robot_idle():
    if (robot_bridge.task and not robot_bridge.task.done()) or (robot_workshop.task and not robot_workshop.task.done()):
        raise HTTPException(409,'A robot operation is active. Stop it before changing scenarios.')

class RobotRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    scenario_id:str
    ambulance:str='AMB-01'
    plan_id:str|None=None

async def robot_operation(action,sid,ambulance,plan_id=None):
    if action=='robot_stop':
        robot_workshop.stop();return robot_bridge.stop()
    async with lock:
        plan=robotics.build_plan(engine.scenario(sid),ambulance)
    if plan_id is not None and plan_id!=plan['id']:
        raise HTTPException(409,'Robot plan is stale. Prepare the current scenario again.')
    if action=='robot_prepare':return robot_bridge.prepare(plan)
    return await robot_bridge.start(plan,'sync' if action=='robot_sync' else 'execute',cyber_lock)

@app.post('/api/robotics/prepare')
async def robot_prepare(req:RobotRequest):
    return await robot_operation('robot_prepare',req.scenario_id,req.ambulance)

@app.post('/api/robotics/sync')
async def robot_sync(req:RobotRequest):
    if not req.plan_id:raise HTTPException(422,'Provide the prepared plan_id.')
    return await robot_operation('robot_sync',req.scenario_id,req.ambulance,req.plan_id)

@app.post('/api/robotics/execute')
async def robot_execute(req:RobotRequest):
    if not req.plan_id:raise HTTPException(422,'Provide the prepared plan_id.')
    return await robot_operation('robot_execute',req.scenario_id,req.ambulance,req.plan_id)

@app.post('/api/robotics/stop')
async def robot_stop():return robot_bridge.stop()

class WorkshopRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    scenario_id:str
    robot_id:str|None=None
    asset_id:str|None=None
    speed:float=Field(default=.24,ge=0,le=.48,allow_inf_nan=False)
    duration:float=Field(default=4,ge=.2,le=10,allow_inf_nan=False)

async def workshop_operation(op,sid):
    async with lock:
        scenario=copy.deepcopy(engine.scenario(sid))
    if op.action=='robot_create':
        return await robot_workshop.launch(lambda cloud:robot_workshop.create(cloud,sid,workshop.mapping_config()),cyber_lock)
    if op.action in ('robot_catalog','robot_design'):
        def request(cloud):
            if op.action=='robot_catalog':return {'status':'catalog','catalog':cloud.catalog(op.robot_description or 'robot'),'verified':False}
            result=cloud.design(op.robot_description)
            result=result.to_dict() if hasattr(result,'to_dict') else result
            return {'status':'design_ready' if result.get('can_build') else 'unsupported','design':result,'verified':False,
                    'detail':'Cyberwave design proposal. Creating an asset and validating simulation are separate steps.'}
        return await robot_workshop.launch(request,cyber_lock)
    record=robot_workshop.selected(op.robot_id,sid)
    fn=(lambda cloud:robot_workshop.test(cloud,record,op.robot_speed,op.robot_duration,scenario)) if op.action=='robot_forward' else (lambda cloud:robot_workshop.observe(cloud,record))
    return await robot_workshop.launch(fn,cyber_lock)

@app.post('/api/workshop/create')
async def workshop_create(req:WorkshopRequest):
    async with lock:
        engine.scenario(req.scenario_id)
    mapping=workshop.mapping_config()
    return await robot_workshop.launch(lambda cloud:robot_workshop.create(cloud,req.scenario_id,mapping,asset_id=req.asset_id),cyber_lock)

@app.post('/api/workshop/upload')
async def workshop_upload(scenario_id:str,file:UploadFile=File(...)):
    async with lock:engine.scenario(scenario_id)
    content=await file.read(2*1024*1024+1)
    schema=await asyncio.to_thread(workshop.validate_upload,content,file.filename or '')
    return await robot_workshop.launch(lambda cloud:robot_workshop.create(cloud,scenario_id,workshop.mapping_config(),schema=schema,source='uploaded'),cyber_lock)

@app.post('/api/workshop/attach')
async def workshop_attach(req:WorkshopRequest):
    async with lock:
        require_robot_idle();engine.scenario(req.scenario_id)
        if req.robot_id not in robot_workshop.robots:raise HTTPException(422,'Workshop robot not found.')
        with robot_workshop.guard:
            robot_workshop.robots[req.robot_id].update(scenario_id=req.scenario_id,verified=False,telemetry=None,mapping=workshop.mapping_config())
            robot_workshop.save()
        return robot_workshop.snapshot()

@app.get('/api/workshop/catalog')
async def workshop_catalog(query:str='robot'):
    if not 1<=len(query)<=80:raise HTTPException(422,'Catalog query must be 1–80 characters.')
    def fetch():
        cloud=workshop.Cloud()
        try:return cloud.catalog(query)
        finally:cloud.close()
    try:return await asyncio.to_thread(fetch)
    except ValueError:raise
    except Exception:raise HTTPException(502,'Cyberwave catalog request failed. Check access and connectivity.')

@app.post('/api/workshop/forward')
async def workshop_forward(req:WorkshopRequest):
    return await workshop_operation(Operation(action='robot_forward',robot_id=req.robot_id,robot_speed=req.speed,robot_duration=req.duration),req.scenario_id)

@app.post('/api/workshop/observe')
async def workshop_observe(req:WorkshopRequest):
    return await workshop_operation(Operation(action='robot_observe',robot_id=req.robot_id),req.scenario_id)

@app.post('/api/workshop/sync')
async def workshop_sync(req:WorkshopRequest):
    async with lock:scenario=copy.deepcopy(engine.scenario(req.scenario_id))
    record=robot_workshop.selected(req.robot_id,req.scenario_id)
    return await robot_workshop.launch(lambda cloud:robot_workshop.sync_scene(cloud,record,scenario),cyber_lock)

@app.post('/api/workshop/preflight')
async def workshop_preflight():
    return await robot_workshop.launch(lambda cloud:robot_workshop.preflight(cloud),cyber_lock)

class SimulationRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    duration:int=Field(default=60,ge=30,le=120)
    accept_cost:bool=False

@app.post('/api/workshop/simulation/start')
async def workshop_simulation_start(req:SimulationRequest):
    if not req.accept_cost:raise HTTPException(422,'Accept the displayed Cyberwave simulation cost before starting.')
    return await robot_workshop.launch(lambda cloud:robot_workshop.start_simulation(cloud,req.duration,req.accept_cost),cyber_lock)

@app.post('/api/workshop/simulation/status')
async def workshop_simulation_status():
    return await robot_workshop.launch(lambda cloud:robot_workshop.simulation_status(cloud),cyber_lock)

@app.post('/api/workshop/simulation/stop')
async def workshop_simulation_stop():
    return await robot_workshop.launch(lambda cloud:robot_workshop.stop_simulation(cloud),cyber_lock)

@app.post('/api/workshop/stop')
async def workshop_stop():return robot_workshop.stop()

@app.post('/api/transcribe')
async def transcribe(file:UploadFile=File(...)):
    content=await file.read(8*1024*1024+1)
    if len(content)>8*1024*1024: raise HTTPException(413,'Audio limit is 8 MiB and 30 seconds.')
    if not content: raise HTTPException(422,'Recording is empty.')
    result=await integrations.transcribe(content,file.filename or 'recording.webm',file.content_type or 'audio/webm')
    provider_status['sarvam'].update(verified=True,detail='Live Saaras v4 transcription succeeded.',request_id=result.get('request_id'))
    return result

@app.post('/api/integrations/cyberwave/probe')
async def cyberwave_probe():
    if cyber_lock.locked(): raise HTTPException(409,'A twin probe is already running.')
    async with cyber_lock:
        result=await integrations.cyberwave_probe()
        provider_status['cyberwave'].update(result)
        return result

# Same origin production UI; Vite proxies /api during development.
static=Path(__file__).resolve().parents[1]/'frontend/dist'
if static.exists(): app.mount('/',StaticFiles(directory=static,html=True),name='frontend')
