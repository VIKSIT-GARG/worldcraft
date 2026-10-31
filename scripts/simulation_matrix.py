"""Run multiple cities and deadline-aware replays over a separate real HTTP server.

Run: .venv/bin/python scripts/simulation_matrix.py
No cloud calls; does not touch the user's running app or credentials.
"""
import json
from pathlib import Path
import socket
import subprocess
import sys
import time

import httpx

root=Path(__file__).resolve().parents[1]
with socket.socket() as sock:
    sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
server=subprocess.Popen([sys.executable,'-m','uvicorn','backend.main:app','--host','127.0.0.1',
                         '--port',str(port),'--log-level','error'],cwd=root,
                        stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
results=[]
try:
    with httpx.Client(base_url=f'http://127.0.0.1:{port}',timeout=15,trust_env=False) as client:
        for _ in range(100):
            try:
                if client.get('/api/health').status_code==200:
                    break
            except httpx.ConnectError:
                pass
            time.sleep(.1)
        else:
            raise RuntimeError('Isolated server failed to start.')

        def post(path,body):
            response=client.post(path,json=body)
            response.raise_for_status()
            return response.json()

        def action(sid,**operation):
            return post('/api/action',{'scenario_id':sid,'operation':operation})

        for buildings in ('ABCD','ABCDEFGHIJKL'):
            for destination in buildings:
                post('/api/control',{'paused':True,'speed':20})
                sid=action(None,action='create_city',buildings=list(buildings),ambulances=4)['scenario_id']
                # Three other vehicles must remain independent of selected AMB-04.
                for index,target in enumerate('BCD',1):
                    action(sid,action='dispatch',ambulance=f'AMB-{index:02}',destination=target,deadline_seconds=1000)
                dispatched=action(sid,action='dispatch',ambulance='AMB-04',destination=destination)
                duration=dispatched['scenarios'][0]['ambulances'][3]['mission']['travel_time']
                deadline=duration+10
                report=action(sid,action='break_plan',ambulance='AMB-04',deadline_seconds=deadline)['analysis']
                assert len(report['closures'])==22 and report['baseline']['success']
                ids=[report[k] for k in ('original_scenario_id','failed_scenario_id','repaired_scenario_id') if report[k]]
                if len(ids)>1:
                    action(sid,action='compare')
                else:
                    action(sid,action='run')
                post('/api/control',{'paused':False})
                limit=time.monotonic()+12
                while time.monotonic()<limit:
                    state=client.get('/api/state');state.raise_for_status();state=state.json()
                    scenarios={sc['id']:sc for sc in state['scenarios']}
                    if all(all(a['mission']['status'] in ('arrived','no_route') for a in scenarios[i]['ambulances']) for i in ids):
                        break
                    time.sleep(.1)
                else:
                    raise AssertionError(f'Replay timed out: {len(buildings)} towers, destination {destination}')
                measured=[]
                for key,result in [('original_scenario_id',report['baseline']),('failed_scenario_id',report['critical']),
                                   ('repaired_scenario_id',report['repair'])]:
                    if not report[key]:
                        continue
                    sc=scenarios[report[key]];mission=sc['ambulances'][3]['mission']
                    assert mission['success']==result['success']
                    assert mission['distance']==result['distance']
                    if result['elapsed'] is not None:
                        assert abs(mission['elapsed']-result['elapsed'])<1e-6
                        assert mission['deadline_met']==result['success']
                    assert [a['mission']['destination'] for a in sc['ambulances'][:3]]==list('BCD')
                    measured.append({'scenario':sc['name'],'distance_m':mission['distance'],
                                     'elapsed_s':mission['elapsed'],'success':mission['success']})
                row={'towers':len(buildings),'ambulances':4,'selected_ambulance':'AMB-04','destination':destination,
                     'deadline_s':deadline,'closures_tested':22,'critical_road':report['critical']['road_id'] if report['critical'] else None,
                     'repair_station':report['repair']['station'] if report['repair'] else None,'replayed':measured}
                results.append(row)
                print(f"PASS {len(buildings)} towers → {destination}: {len(ids)} scenarios replayed; 22 closures tested",flush=True)

        # Reject impossible baselines and unsupported operations without modifying scenarios.
        post('/api/control',{'paused':True})
        sid=action(None,action='create_city')['scenario_id']
        action(sid,action='dispatch',deadline_seconds=1)
        before=client.get('/api/state').json()['scenarios']
        assert client.post('/api/action',json={'scenario_id':sid,'operation':{'action':'break_plan'}}).status_code==422
        assert client.get('/api/state').json()['scenarios']==before
        assert client.post('/api/action',json={'scenario_id':sid,'operation':{'action':'execute_code','code':'print(1)'}}).status_code==422
        action(sid,action='set_deadline',deadline_seconds=65)
        action(sid,action='restrict',road='main');action(sid,action='restrict',road='local')
        assert client.post('/api/action',json={'scenario_id':sid,'operation':{'action':'break_plan'}}).status_code==422
        evidence={'result':'passed','transport':'isolated Uvicorn + actual HTTP + background simulation clock',
                  'cloud_calls':False,'city_configurations':len(results),'single_road_closure_trials':sum(r['closures_tested'] for r in results),
                  'scenario_replays':sum(len(r['replayed']) for r in results),
                  'negative_checks':['infeasible deadline rejected atomically','unsupported operation rejected','unreachable baseline rejected'],
                  'results':results}
        (root/'docs/simulation-matrix-results.json').write_text(json.dumps(evidence,indent=2)+'\n')
        print(json.dumps({k:v for k,v in evidence.items() if k!='results'},indent=2))
finally:
    server.terminate()
    try:
        server.wait(timeout=5)
    except subprocess.TimeoutExpired:
        server.kill();server.wait()
