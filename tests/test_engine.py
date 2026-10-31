import math
import pytest
from backend.engine import Engine, Operation

@pytest.fixture
def demo():
    e=Engine();sid=e.apply(Operation(action='create_city'));s=e.scenario(sid)
    e.apply(Operation(action='dispatch'),sid)
    return e,sid,s

def test_default_world_and_a_star(demo):
    e,sid,s=demo
    assert len(s['world']['buildings'])==5
    assert len(s['ambulances'])==2
    m=s['ambulances'][0]['mission']
    assert m['distance']==480
    assert m['travel_time']==40
    assert all(x['kind']=='main' for x in m['segments'])

def test_main_road_constraint_and_independent_comparison(demo):
    e,sid,s=demo
    e.apply(Operation(action='restrict'),sid)
    m=s['ambulances'][0]['mission']
    assert m['distance']==720
    assert m['travel_time']==90
    assert all(x['kind']=='local' for x in m['segments'])
    other=e.apply(Operation(action='clone'),sid)
    e.apply(Operation(action='unrestrict'),other)
    assert s['blocked']==['main']
    assert e.scenario(other)['blocked']==[]
    e.scenario(other)['world']['buildings'][0]['label']='Independent hospital'
    assert s['world']['buildings'][0]['label']=='Hospital'
    e.apply(Operation(action='compare'),sid)
    e.tick(40)
    slow=s['ambulances'][0]['mission'];fast=e.scenario(other)['ambulances'][0]['mission']
    assert fast['success'] and fast['elapsed']==40 and fast['travelled']==480
    assert slow['status']=='running' and not slow['success']
    e.tick(50)
    assert slow['success'] and slow['elapsed']==90 and slow['travelled']==720

def test_mid_edge_reroute_preserves_pose_time_and_egress(demo):
    e,sid,s=demo;e.run(s);e.tick(3)
    a=s['ambulances'][0];position=a['position'].copy()
    assert position==[-204.,0.]
    e.restrict(s,'main',True)
    assert a['position']==position
    assert a['mission']['elapsed']==3
    assert a['mission']['egress']
    assert a['mission']['segments'][0]['kind']=='egress'
    assert all(x['kind']!='main' for x in a['mission']['segments'])
    e.tick(300)
    assert a['mission']['success']
    assert a['position']==[240,0]

def test_all_roads_blocked_reports_failure(demo):
    e,sid,s=demo
    e.restrict(s,'main',True);e.restrict(s,'local',True);e.run(s);e.tick(100)
    m=s['ambulances'][0]['mission']
    assert m['status']=='no_route' and m['distance'] is None and m['success'] is False
    assert s['ambulances'][0]['position']==[-240,0]
    e.restrict(s,'local',False)
    assert m['status']=='running'

def test_deterministic_tick_size(demo):
    e,sid,s=demo;e.run(s)
    for _ in range(800):e.tick(.05)
    assert s['ambulances'][0]['mission']['success']
    assert math.isclose(s['ambulances'][0]['mission']['elapsed'],40,abs_tol=1e-7)

def test_comparison_restarts_common_origin(demo):
    e,sid,s=demo;e.run(s);e.tick(5)
    other=e.clone(s,'Branch');e.restrict(s,'main',True)
    e.apply(Operation(action='compare'),sid)
    for sc in e.scenarios.values():
        assert sc['ambulances'][0]['position']==[-240,0]
        assert sc['ambulances'][0]['mission']['elapsed']==0
    assert s['ambulances'][0]['mission']['distance']==720
    assert e.scenario(other)['ambulances'][0]['mission']['distance']==480

def test_multi_vehicle_independence(demo):
    e,sid,s=demo
    e.dispatch(s,Operation(action='dispatch',ambulance='AMB-02',destination='B'))
    e.run(s);e.tick(15)
    a,b=s['ambulances']
    assert a['position']!=b['position']
    assert a['mission']['target']!=b['mission']['target']

@pytest.mark.parametrize('names',[['A','a'],[''],['hospital']])
def test_invalid_world_names(names):
    with pytest.raises(ValueError): Engine().create(names,2)

def test_reject_unknown_target_without_mutation(demo):
    e,sid,s=demo;before=e.snapshot()
    with pytest.raises(ValueError):e.apply(Operation(action='dispatch',destination='Z'),sid)
    assert e.snapshot()==before

def test_schema_rejects_unbounded_or_invented_actions():
    for data in [{'action':'exec','code':'x'},{'action':'create_city','ambulances':100},{'action':'restrict','shell':'rm'}]:
        with pytest.raises(ValueError):Operation.model_validate(data)
