"""Launch production app and verify the mandatory flow over real HTTP."""
import json, os, socket, subprocess, sys, time
from pathlib import Path
import httpx
root=Path(__file__).resolve().parents[1]
with socket.socket() as sock:
    sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
env=os.environ.copy()
p=subprocess.Popen([sys.executable,'-m','uvicorn','backend.main:app','--host','127.0.0.1','--port',str(port)],cwd=root,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
try:
    with httpx.Client(base_url=f'http://127.0.0.1:{port}',timeout=15,trust_env=False) as c:
        for _ in range(80):
            try:
                if c.get('/api/health').status_code==200:break
            except httpx.ConnectError:pass
            time.sleep(.1)
        else:raise RuntimeError('App did not become healthy.')
        assert c.get('/').status_code==200 and 'WORLDCRAFT' in c.get('/').text
        assert c.get('/openapi.json').status_code==200
        assert c.post('/api/control',json={'paused':True,'speed':20}).status_code==200
        def cmd(text,sid=None):
            r=c.post('/api/command',json={'text':text,'mode':'local','scenario_id':sid});r.raise_for_status();return r.json()
        sid=cmd('Create city')['scenario_id']
        cmd('Send an ambulance from the hospital to Tower A.',sid)
        cmd("Don't use the main road.",sid)
        other=cmd('Create a parallel scenario',sid)['scenario_id']
        cmd('Allow main road',other);cmd('Compare scenarios',sid)
        c.post('/api/control',json={'paused':False})
        deadline=time.monotonic()+9
        while time.monotonic()<deadline:
            state=c.get('/api/state').json();missions=[s['ambulances'][0]['mission'] for s in state['scenarios']]
            if all(m['success'] for m in missions):break
            time.sleep(.2)
        assert all(m['success'] for m in missions),missions
        assert [m['distance'] for m in missions]==[720,480]
        assert all(abs(m['elapsed']-t)<1e-6 for m,t in zip(missions,[90,40]))
        result={'transport':'actual HTTP + running Uvicorn + background simulation clock','result':'passed','planner':'local (no AI)','scenarios':[{'name':s['name'],'distance_m':m['distance'],'simulated_travel_time_s':round(m['elapsed'],3),'success':m['success']} for s,m in zip(state['scenarios'],missions)],'integrations':state['integrations']}
        c.post('/api/control',json={'paused':True}).raise_for_status()
        sid=cmd('Create city')['scenario_id']
        cmd('Send an ambulance from the hospital to Tower A.',sid)
        response=c.post('/api/action',json={'scenario_id':sid,'operation':{'action':'break_plan','deadline_seconds':65}})
        response.raise_for_status()
        analysis=response.json()['analysis']
        assert len(analysis['closures'])==22
        assert analysis['baseline']['elapsed']==40 and analysis['baseline']['distance']==480
        assert analysis['critical']['elapsed']==90 and analysis['critical']['distance']==720
        assert analysis['repair']['elapsed']==25 and analysis['repair']['success']
        cmd('Compare scenarios',sid)
        c.post('/api/control',json={'paused':False}).raise_for_status()
        deadline=time.monotonic()+9
        while time.monotonic()<deadline:
            state=c.get('/api/state').json()
            missions=[s['ambulances'][0]['mission'] for s in state['scenarios']]
            if all(m['status']=='arrived' for m in missions):break
            time.sleep(.2)
        assert [m['elapsed'] for m in missions]==[40,90,25],missions
        assert [m['success'] for m in missions]==[True,False,True],missions
        assert [m['deadline_met'] for m in missions]==[True,False,True]
        result['break_my_plan']={'deadline_seconds':65,'road_closures_tested':len(analysis['closures']),
            'critical_road':analysis['critical']['road_id'],'repair_station':analysis['repair']['station'],
            'replayed_scenarios':[{'name':s['name'],'distance_m':m['distance'],'elapsed_s':m['elapsed'],
                                  'deadline_met':m['deadline_met']} for s,m in zip(state['scenarios'],missions)]}
        print(json.dumps(result,indent=2));(root/'docs/http-smoke-results.json').write_text(json.dumps(result,indent=2)+'\n')
finally:
    p.terminate()
    try:p.wait(timeout=5)
    except subprocess.TimeoutExpired:p.kill();p.wait()
