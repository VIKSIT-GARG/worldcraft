"""Exercise real simulations against a separate uniform-road BFS oracle."""
import copy
from collections import deque

import pytest

from backend.engine import Engine, Operation


def reference_route(world, blocked, origin, target):
    """Shortest hop count and all possible travel times at that hop count.

    Deliberately independent of Engine.graph/plan and NetworkX A*.
    The generated city has uniform 120 m road segments but two speeds.
    """
    neighbours={n:[] for n in world['nodes']}
    for road in world['roads']:
        assert road['length']==120
        if road['id'] in blocked or road['kind'] in blocked:
            continue
        seconds=10 if road['kind']=='main' else 15
        neighbours[road['a']].append((road['b'],seconds))
        neighbours[road['b']].append((road['a'],seconds))
    depths={origin:0}; times={origin:{0}}; queue=deque([origin])
    while queue:
        node=queue.popleft()
        for other,seconds in neighbours[node]:
            depth=depths[node]+1
            if other not in depths:
                depths[other]=depth;times[other]=set();queue.append(other)
            if depths[other]==depth:
                times[other].update(t+seconds for t in times[node])
    return (120*depths[target],times[target]) if target in depths else (None,set())


@pytest.mark.parametrize('destination',list('ABCDEFGHIJKL'))
@pytest.mark.parametrize('ambulances',[1,4])
@pytest.mark.parametrize('blocked',[None,'main','local'])
@pytest.mark.parametrize('deadline_mode',['tight','rescue','generous'])
def test_all_destinations_closures_deadlines_and_fleet_sizes(destination,ambulances,blocked,deadline_mode):
    engine=Engine();sid=engine.create(list('ABCDEFGHIJKL'),ambulances)
    source=engine.scenario(sid)
    if blocked:
        engine.apply(Operation(action='restrict',road=blocked),sid)
    selected=f'AMB-{ambulances:02}'
    engine.apply(Operation(action='dispatch',destination=destination,ambulance=selected),sid)
    selected_mission=engine.vehicle(source,selected)['mission']
    deadline={'tight':1,'rescue':(selected_mission['travel_time'] or 0)+10,'generous':1000}[deadline_mode]
    engine.apply(Operation(action='set_deadline',ambulance=selected,deadline_seconds=deadline),sid)
    if ambulances>1:
        engine.apply(Operation(action='dispatch',destination='B',ambulance='AMB-01',deadline_seconds=1000),sid)
    engine.tick(2)  # Analysis must not overwrite the live, partly travelled mission.
    before=copy.deepcopy(source)
    distance,times=reference_route(source['world'],source['blocked'],'-240:0',selected_mission['target'])
    if not times or selected_mission['travel_time']>deadline:
        snapshot=engine.snapshot()
        with pytest.raises(ValueError,match='baseline'):
            engine.break_plan(sid,selected)
        assert engine.snapshot()==snapshot
        return
    report=engine.break_plan(sid,selected)
    assert source==before
    assert report['baseline']['distance']==distance
    assert report['baseline']['elapsed'] in times
    assert len(report['closures'])==22
    assert len({r['road_id'] for r in report['closures']})==22
    for result in report['closures']:
        distance,times=reference_route(source['world'],[*source['blocked'],result['road_id']],
                                      result['origin'],result['destination'])
        assert result['distance']==distance
        if not times:
            assert result['failure']=='no_route' and result['elapsed'] is None and not result['success']
        else:
            assert result['elapsed'] in times
            assert result['success']==(result['elapsed']<=deadline)
            assert result['failure']==(None if result['success'] else 'deadline_missed')
    failures=[r for r in report['closures'] if not r['success']]
    assert bool(report['critical'])==bool(failures)
    if not failures:
        assert report['repair'] is None and len(engine.scenarios)==1
        return
    critical=report['critical']
    if any(r['failure']=='no_route' for r in failures):
        assert critical['failure']=='no_route'
    else:
        assert critical['elapsed']==max(r['elapsed'] for r in failures)
    for repair in report['repair_candidates']:
        distance,times=reference_route(source['world'],[*source['blocked'],critical['road_id']],
                                      repair['origin'],repair['destination'])
        assert repair['origin'] not in (critical['origin'],critical['destination'])
        assert repair['distance']==distance
        assert (repair['elapsed'] in times) if times else repair['elapsed'] is None
        assert repair['success']==bool(times and repair['elapsed']<=deadline)
    successful=[r for r in report['repair_candidates'] if r['success']]
    if successful:
        assert report['repair']['success']
        assert report['repair']['elapsed']==min(r['elapsed'] for r in successful)
    # Replay must preserve the measured outcomes, including every other ambulance.
    engine.apply(Operation(action='compare'),sid)
    engine.tick(1000)
    for key,result in [('original_scenario_id',report['baseline']),('failed_scenario_id',critical),
                       ('repaired_scenario_id',report['repair'])]:
        scenario=engine.scenario(report[key]); m=engine.vehicle(scenario,selected)['mission']
        assert m['success']==result['success']
        assert m['distance']==result['distance']
        if result['elapsed'] is not None:
            assert m['elapsed']==pytest.approx(result['elapsed'])
        if ambulances>1:
            assert scenario['ambulances'][0]['mission']['destination']=='B'


@pytest.mark.parametrize('destination',list('ABCDEFGHIJKL'))
def test_reopening_after_arrival_and_replaying_three_times(destination):
    engine=Engine();sid=engine.create(list('ABCDEFGHIJKL'),1)
    engine.apply(Operation(action='restrict',road='main'),sid)
    engine.apply(Operation(action='dispatch',destination=destination),sid)
    engine.tick(1000)
    branch=engine.apply(Operation(action='clone',name='Reopened'),sid)
    engine.apply(Operation(action='unrestrict',road='main'),branch)
    for _ in range(3):
        engine.apply(Operation(action='compare'),branch)
        engine.tick(1000)
        for sc in engine.scenarios.values():
            mission=sc['ambulances'][0]['mission']
            distance,times=reference_route(sc['world'],sc['blocked'],mission['origin'],mission['target'])
            assert mission['distance']==distance and mission['elapsed'] in times
            assert mission['success'] and mission['travelled']==distance
    assert engine.scenario(sid)['blocked']==['main']
    assert engine.scenario(branch)['blocked']==[]


def test_repeated_analysis_capacity_rejection_is_atomic():
    engine=Engine();sid=engine.create(list('ABCD'),1)
    engine.apply(Operation(action='dispatch',deadline_seconds=65),sid)
    engine.break_plan(sid);engine.break_plan(sid)
    before=engine.snapshot()
    with pytest.raises(ValueError,match='slots'):
        engine.break_plan(sid)
    assert engine.snapshot()==before
