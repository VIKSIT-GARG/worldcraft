"""Robot bridge contract tests with fake transport; never live-motion evidence."""
import copy
import threading
import time

import pytest

from backend.engine import Engine, Operation
from backend import robotics


@pytest.fixture
def plan(monkeypatch):
    monkeypatch.setenv('CYBERWAVE_ENVIRONMENT_ID','test-env')
    monkeypatch.setenv('CYBERWAVE_TWIN_ID','test-twin')
    monkeypatch.setenv('CYBERWAVE_API_KEY','fake-test-key')
    engine=Engine();sid=engine.create(list('ABCD'),2)
    engine.apply(Operation(action='dispatch',deadline_seconds=65),sid)
    return robotics.build_plan(engine.scenario(sid),'AMB-01')


def test_mapping_and_plan_do_not_mutate_city(plan):
    assert plan['waypoints'][0]==[20,20,0.35]
    assert plan['waypoints'][-1]==[44,20,0.35]
    assert plan['distance_m']==480 and plan['robot_distance_m']==24
    assert plan['mode']=='simulation' and plan['source_type']=='sim_tele'
    assert all(o['fixed_base'] for o in plan['objects'])
    assert all(o['parameters']['disable_collision'] for o in plan['objects'] if o['metadata']['role']=='road')
    assert 'fake-test-key' not in str(plan)


def test_constraints_change_plan_and_scene_hash(plan):
    engine=Engine();sid=engine.create(list('ABCD'),2)
    engine.apply(Operation(action='dispatch'),sid)
    before=copy.deepcopy(engine.scenario(sid))
    open_plan=robotics.build_plan(engine.scenario(sid),'AMB-01')
    assert engine.scenario(sid)==before
    engine.apply(Operation(action='restrict',road='main'),sid)
    closed=robotics.build_plan(engine.scenario(sid),'AMB-01')
    assert closed['id']!=open_plan['id'] and closed['scene_hash']!=open_plan['scene_hash']
    assert closed['distance_m']==720 and closed['robot_distance_m']==36
    assert len([o for o in closed['objects'] if o['metadata']['role']=='barrier'])==4


def test_impossible_route_rejected():
    engine=Engine();sid=engine.create(list('ABCD'),1)
    for road in ('main','local'):
        engine.apply(Operation(action='restrict',road=road),sid)
    engine.apply(Operation(action='dispatch'),sid)
    with pytest.raises(ValueError,match='route'):
        robotics.build_plan(engine.scenario(sid),'AMB-01')


class FakeRobot:
    """Emulate measured pose separately from navigation acceptance."""
    def __init__(self,plan,*,fresh=True,moves=True):
        self.plan=plan;self.fresh=fresh;self.moves=moves;self.sent=[];self.stopped=False
        self.position=plan['waypoints'][0];self.callback=None

    def subscribe(self,callback):
        self.callback=callback
        if self.fresh:self.emit()

    def emit(self):
        self.callback({'source_type':'sim','timestamp':time.time(),
                       'position':dict(zip(('x','y','z'),self.position))})

    def goto(self,point):
        self.sent.append(point)
        if self.moves:
            self.position=point;self.emit()
        return {'action_id':'fake-accepted'}

    def stop(self):self.stopped=True


def test_execution_requires_fresh_telemetry_before_any_command(plan):
    robot=FakeRobot(plan,fresh=False);events=[]
    result=robotics.execute_route(plan,robot,threading.Event(),events.append,initial_timeout=.01)
    assert result['status']=='blocked' and not result['verified']
    assert robot.sent==[]


def test_acceptance_without_measured_arrival_is_not_success(plan):
    robot=FakeRobot(plan,moves=False)
    result=robotics.execute_route(plan,robot,threading.Event(),lambda x:None,max_seconds=.02)
    assert result['status']=='timed_out' and not result['verified']
    assert len(robot.sent)==1 and robot.stopped


def test_route_executes_each_waypoint_using_measured_pose(plan):
    robot=FakeRobot(plan);events=[]
    result=robotics.execute_route(plan,robot,threading.Event(),events.append)
    assert result['status']=='completed' and result['verified']
    assert robot.sent==plan['waypoints'][1:] and robot.stopped
    assert result['reached_waypoints']==len(plan['waypoints'])-1
    assert any(e.get('telemetry') for e in events)


def test_start_mismatch_and_cancel_do_not_dispatch(plan):
    robot=FakeRobot(plan);robot.position=[0,0,.35]
    result=robotics.execute_route(plan,robot,threading.Event(),lambda x:None)
    assert result['status']=='blocked' and not robot.sent
    cancel=threading.Event();cancel.set();robot=FakeRobot(plan)
    result=robotics.execute_route(plan,robot,cancel,lambda x:None)
    assert result['status']=='cancelled' and not robot.sent


def test_editor_pose_is_not_simulator_evidence(plan):
    robot=FakeRobot(plan,fresh=False)
    def subscribe(callback):
        callback({'source_type':'edit','timestamp':time.time(),'position':{'x':20,'y':20,'z':.35}})
    robot.subscribe=subscribe
    result=robotics.execute_route(plan,robot,threading.Event(),lambda x:None,initial_timeout=.01)
    assert result['status']=='blocked' and not robot.sent


def test_stop_is_attempted_when_submission_raises(plan):
    robot=FakeRobot(plan)
    def broken(point):raise RuntimeError('transport failed')
    robot.goto=broken
    result=robotics.execute_route(plan,robot,threading.Event(),lambda x:None)
    assert result['status']=='failed' and not result['verified'] and robot.stopped


def test_stale_prepared_plan_rejected_through_api(plan,monkeypatch):
    from backend import main
    from fastapi.testclient import TestClient
    monkeypatch.setattr(main,'engine',Engine())
    monkeypatch.setattr(main,'robot_bridge',robotics.RobotBridge())
    with TestClient(main.app) as client:
        client.post('/api/control',json={'paused':True})
        sid=client.post('/api/action',json={'operation':{'action':'create_city'}}).json()['scenario_id']
        client.post('/api/action',json={'scenario_id':sid,'operation':{'action':'dispatch'}})
        request={'scenario_id':sid,'ambulance':'AMB-01'}
        prepared=client.post('/api/robotics/prepare',json=request)
        assert prepared.status_code==200 and prepared.json()['plan']['mode']=='simulation'
        request['plan_id']=prepared.json()['plan']['id']
        client.post('/api/action',json={'scenario_id':sid,'operation':{'action':'restrict'}})
        response=client.post('/api/robotics/execute',json=request)
        assert response.status_code==409 and 'stale' in response.json()['detail']
        response=client.post('/api/robotics/execute',json={'scenario_id':sid})
        assert response.status_code==422
        assert client.post('/api/robotics/stop').status_code==200


def test_voice_robot_operations_use_the_bridge(plan,monkeypatch):
    from backend import main,integrations
    from fastapi.testclient import TestClient
    engine=Engine();sid=engine.create(list('ABCD'),1);engine.apply(Operation(action='dispatch'),sid)
    monkeypatch.setattr(main,'engine',engine)
    monkeypatch.setattr(main,'robot_bridge',robotics.RobotBridge())
    monkeypatch.setattr(integrations,'gemini_plan',lambda text,context:(Operation(action='robot_prepare'),'mock-response'))
    with TestClient(main.app) as client:
        r=client.post('/api/command',json={'scenario_id':sid,'mode':'gemini','text':'रोबोट के लिए रास्ता तैयार करो'})
        assert r.status_code==200
        assert r.json()['robotics_result']['plan']['distance_m']==480
        assert r.json()['robotics_result']['job']['verified'] is False


def test_missing_configuration_is_blocked_without_motion(plan,monkeypatch):
    monkeypatch.delenv('CYBERWAVE_API_KEY')
    with pytest.raises(ValueError,match='Configure'):
        robotics.CyberwaveRobot(plan)


def test_scene_sdk_serialization_preserves_position(plan):
    # Generated SDK Position is an anyOf wrapper: passing a raw dict loses X/Y.
    obj=plan['objects'][0]
    for patch in (False,True):
        payload=robotics.primitive_model(obj,patch=patch).to_dict()
        assert payload['position']=={'x':22.25,'y':17.75,'z':.55}


def test_scene_verification_accepts_defaults_but_rejects_geometry_changes(plan):
    from types import SimpleNamespace
    stored=copy.deepcopy(plan['objects'])
    for o in stored:o['parameters']['material']='matte'
    robot=object.__new__(robotics.CyberwaveRobot);robot.plan=plan
    robot.scene=lambda:SimpleNamespace(settings={'procedural_primitives':stored})
    robot.verify_scene()
    stored[0]['position']['x']=0
    with pytest.raises(ValueError,match='changed'):
        robot.verify_scene()


def test_robot_buildings_leave_the_navigation_lanes_clear(plan):
    def overlaps(a,b):
        return all(abs(a['position'][axis]-b['position'][axis]) <
                   (a['parameters'][size]+b['parameters'][size])/2+.2
                   for axis,size in [('x','width'),('y','depth')])
    buildings=[o for o in plan['objects'] if o['metadata']['role']=='building']
    roads=[o for o in plan['objects'] if o['metadata']['role']=='road']
    assert not [(b['id'],r['id']) for b in buildings for r in roads if overlaps(b,r)]


def test_cancel_during_navigation_sends_stop(plan):
    cancel=threading.Event();robot=FakeRobot(plan,moves=False)
    def publish(event):
        if event['status']=='running':cancel.set()
    result=robotics.execute_route(plan,robot,cancel,publish)
    assert result['status']=='cancelled' and robot.stopped and not result['verified']


def test_stop_failure_prevents_verified_success(plan):
    robot=FakeRobot(plan)
    def broken_stop():raise RuntimeError('stop transport failed')
    robot.stop=broken_stop
    result=robotics.execute_route(plan,robot,threading.Event(),lambda x:None)
    assert result['status']=='failed' and result['stop_sent'] is False and not result['verified']


def test_sdk_navigation_transport_is_pinned_to_simulation(plan):
    from types import SimpleNamespace
    from cyberwave.motion import TwinNavigationHandle
    sent=[]
    class API:
        def param_serialize(self,**kwargs):sent.append(kwargs);return ()
        def call_api(self,*args):return SimpleNamespace(read=lambda:None,data=b'{"action_id":"mock-id"}')
    twin=SimpleNamespace(uuid='test-twin',client=SimpleNamespace(api=SimpleNamespace(api_client=API())))
    transport=object.__new__(robotics.CyberwaveRobot);transport.plan=plan
    transport.robot=SimpleNamespace(navigation=TwinNavigationHandle(twin))
    transport.goto(plan['waypoints'][1]);transport.stop()
    assert [s['body']['command'] for s in sent]==['goto','stop']
    assert all(s['body']['source_type']=='sim_tele' and s['body']['environment_uuid']=='test-env' for s in sent)
    assert all(s['resource_path']=='/api/v1/twins/test-twin/navigation' for s in sent)
    assert sent[0]['body']['position']==plan['waypoints'][1]


def test_bridge_serializes_control_and_releases_it_after_stop(plan,monkeypatch):
    import asyncio
    robot=FakeRobot(plan,moves=False)
    robot.verify_scene=lambda:None
    robot.close=lambda:None
    monkeypatch.setattr(robotics,'CyberwaveRobot',lambda plan:robot)
    async def exercise():
        bridge=robotics.RobotBridge();bridge.prepare(plan);mutex=asyncio.Lock()
        await bridge.start(plan,'execute',mutex)
        await asyncio.sleep(.02)
        with pytest.raises(ValueError,match='active'):
            await bridge.start(plan,'execute',mutex)
        with pytest.raises(ValueError,match='active'):
            bridge.prepare(plan)
        bridge.stop();await bridge.task
        assert not mutex.locked() and robot.stopped
        assert bridge.snapshot()['job']['status']=='cancelled'
        assert not bridge.snapshot()['job']['verified']
    asyncio.run(exercise())


def test_preparing_new_route_clears_previous_motion_evidence(plan):
    bridge=robotics.RobotBridge()
    bridge.result={'status':'completed','verified':True,'telemetry':{'position':[1,2,3]}}
    bridge.prepare(plan)
    state=bridge.snapshot()
    assert state['job']['status']=='prepared'
    assert state['job']['verified'] is False and state['job']['telemetry'] is None
