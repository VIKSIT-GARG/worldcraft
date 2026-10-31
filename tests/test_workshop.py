"""Custom model tests use real local physics, never fabricated cloud evidence."""
import copy
import pytest

from backend import workshop


def test_custom_rover_drives_and_brakes_in_real_local_mujoco():
    result=workshop.validate_rover()
    assert result['runtime']=='local MuJoCo'
    assert result['forward_distance_m']>.3
    assert abs(result['lateral_distance_m'])<.05
    assert result['stopped_speed_m_s']<.03
    assert result['cloud_verified'] is False


def test_forward_controller_rejects_reverse_turn_and_unbounded_commands():
    assert workshop.forward_controls(.24)=={'left_drive':3,'right_drive':3}
    for speed in [-.1,float('nan'),float('inf'),2]:
        with pytest.raises(ValueError):workshop.forward_controls(speed)


def test_exported_schema_preserves_dynamics():
    schema=workshop.rover_schema()
    assert len(schema['actuators'])==2
    assert all(a['type']=='velocity' and a['control_range']==[0,6] for a in schema['actuators'])
    assert len([j for j in schema['joints'] if j['name'] in ('left_drive','right_drive')])==2
    assert 'test_floor' not in str(schema['links'])
    assert schema['extensions']['cyberwave']['capabilities']['has_joints']


def test_upload_rejects_external_meshes_and_invalid_json():
    for data in [b'bad json',b'{"links":[{"visuals":[{"geometry":{"filename":"/etc/passwd"}}]}]}']:
        with pytest.raises(ValueError):workshop.validate_upload(data,'robot.json')


class FakeRobot:
    def __init__(self,moves=False,source='sim'):
        from types import SimpleNamespace
        self.callback=None;self.position=0;self.moves=moves;self.source=source;self.commands=[]
        self.joints=SimpleNamespace(set=self.set)
    def subscribe_position(self,callback):self.callback=callback;self.emit()
    def emit(self):
        import time
        self.callback({'source_type':self.source,'timestamp':time.time(),'position':{'x':self.position,'y':0,'z':.08}})
    def set(self,values,**kwargs):
        self.commands.append((values,kwargs))
        if self.moves and values['left_drive']>0:self.position+=.03
        self.emit()


@pytest.mark.parametrize('moves',[False,True])
def test_cloud_command_acceptance_requires_measured_motion(moves):
    import threading
    robot=FakeRobot(moves=moves)
    result=workshop.run_forward(robot,'forward_only',.24,.2,threading.Event(),lambda x:None)
    assert result['verified']==moves
    assert result['status']==('completed' if moves else 'unverified')
    assert result['stop_sent']
    assert all(kw['source_type']=='sim_tele' and kw['what_data']=='velocity' for _,kw in robot.commands)
    assert robot.commands[-1][0]=={'left_drive':0,'right_drive':0}


def test_editor_pose_does_not_authorize_forward_command():
    import threading
    robot=FakeRobot(source='edit')
    result=workshop.run_forward(robot,'forward_only',.24,.2,threading.Event(),lambda x:None,initial_timeout=.01)
    assert result['status']=='blocked' and not result['verified'] and not robot.commands


def test_forward_cancel_sends_zero_to_both_motors():
    import threading
    cancel=threading.Event();robot=FakeRobot(moves=True)
    def publish(data):
        if data['status']=='running':cancel.set()
    result=workshop.run_forward(robot,'forward_only',.24,1,cancel,publish)
    assert result['status']=='cancelled' and not result['verified']
    assert robot.commands[-1][0]=={'left_drive':0,'right_drive':0}


def test_persisted_library_and_history_do_not_share_objects(tmp_path):
    path=tmp_path/'workshop.json';one=workshop.Workshop(path)
    one.robots={'r':{'name':'robot','allowed_actions':['observe']}};one.tests=[{'verified':False}];one.save()
    two=workshop.Workshop(path);two.robots['r']['name']='changed'
    assert one.robots['r']['name']=='robot'
    assert two.tests==[{'verified':False}]


def test_api_rejects_cost_without_contacting_cloud(tmp_path,monkeypatch):
    from backend import main
    from fastapi.testclient import TestClient
    monkeypatch.setattr(main,'robot_workshop',workshop.Workshop(tmp_path/'robots.json'))
    def forbidden():raise AssertionError('Cloud must not be contacted before cost acceptance')
    monkeypatch.setattr(workshop,'Cloud',forbidden)
    with TestClient(main.app) as client:
        response=client.post('/api/workshop/simulation/start',json={'duration':60})
        assert response.status_code==422


def test_ineligible_cloud_simulation_is_not_started(tmp_path):
    from types import SimpleNamespace
    def forbidden(*args,**kw):raise AssertionError('Ineligible run must not start')
    cloud=SimpleNamespace(environment='env',preflight=lambda:{'eligible':False,'reasons':['unsupported']},
        client=SimpleNamespace(environments=SimpleNamespace(simulations=SimpleNamespace(list=lambda env:[],start=forbidden))))
    result=workshop.Workshop(tmp_path/'r.json').start_simulation(cloud,60,True)
    assert result['status']=='blocked' and not result['verified']


def test_hindi_creation_routes_to_real_schema_builder_with_fake_cloud(tmp_path,monkeypatch):
    import time
    from types import SimpleNamespace
    from backend import main,integrations
    from backend.engine import Engine,Operation
    from fastapi.testclient import TestClient
    received=[]
    class Cloud:
        environment='fake-env'
        def create(self,schema,name,source):
            received.append(schema)
            return SimpleNamespace(uuid='fake-asset',name=name,capabilities={'has_joints':True})
        def instantiate(self,*args):return 'fake-twin'
        def close(self):pass
    engine=Engine();sid=engine.create(list('ABCD'),1)
    monkeypatch.setattr(main,'engine',engine)
    monkeypatch.setattr(main,'robot_workshop',workshop.Workshop(tmp_path/'r.json'))
    monkeypatch.setattr(workshop,'Cloud',Cloud)
    monkeypatch.setattr(integrations,'gemini_plan',lambda text,context:(Operation(action='robot_create'),'fake-planner'))
    with TestClient(main.app) as client:
        response=client.post('/api/command',json={'scenario_id':sid,'mode':'gemini','text':'मुझे एक रोबोट चाहिए जो सिर्फ आगे चले'})
        assert response.status_code==200
        for _ in range(100):
            state=client.get('/api/state').json()['workshop']
            if not state['active']:break
            time.sleep(.01)
        assert state['job']['status']=='created' and not state['job']['verified']
        robot=state['robots'][0]
        assert robot['twin_id']=='fake-twin' and robot['profile']=='forward_only'
        assert robot['allowed_actions']==['forward','stop','observe']
        assert len(received[0]['actuators'])==2
        assert all(a['control_range']==[0,6] for a in received[0]['actuators'])


def test_workshop_scene_is_independent_of_ambulance_route():
    from backend.engine import Engine,Operation
    from backend.robotics import verify_scene_objects
    engine=Engine();sid=engine.create(list('ABCD'),1)
    record={'scenario_id':sid,'environment_id':'env','twin_id':'twin','mapping':workshop.mapping_config()}
    before=copy.deepcopy(engine.scenario(sid))
    plan=workshop.scene_plan(record,engine.scenario(sid))
    assert engine.scenario(sid)==before
    assert plan['waypoints'][0]==[20,-20,.35]
    stored={'procedural_primitives':plan['objects']}
    engine.apply(Operation(action='restrict',road='main'),sid)
    changed=workshop.scene_plan(record,engine.scenario(sid))
    with pytest.raises(ValueError,match='changed|missing'):
        verify_scene_objects(changed,stored)
    engine.apply(Operation(action='restrict',road='local'),sid)
    all_blocked=workshop.scene_plan(record,engine.scenario(sid))
    assert len([o for o in all_blocked['objects'] if o['metadata']['role']=='barrier'])==22
