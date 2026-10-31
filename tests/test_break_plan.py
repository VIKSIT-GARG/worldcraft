import copy
import pytest
from backend.engine import Engine, Operation


@pytest.fixture
def mission():
    engine=Engine(); sid=engine.apply(Operation(action='create_city'))
    engine.apply(Operation(action='dispatch',destination='A',deadline_seconds=65),sid)
    return engine,sid


def test_deadline_is_inclusive_and_late_arrival_is_not_success(mission):
    engine,sid=mission
    vehicle=engine.scenario(sid)['ambulances'][0]
    engine.restrict(engine.scenario(sid),'main',True)
    engine.tick(90)
    assert vehicle['mission']['status']=='arrived'
    assert vehicle['mission']['elapsed']==90
    assert vehicle['mission']['success'] is False
    assert vehicle['mission']['deadline_met'] is False
    engine.reset_missions(engine.scenario(sid))
    engine.restrict(engine.scenario(sid),'main',False)
    engine.run(engine.scenario(sid));engine.tick(40)
    assert vehicle['mission']['success'] is True
    assert vehicle['mission']['deadline_met'] is True


def test_break_plan_checks_every_single_road_and_creates_independent_repair(mission):
    engine,sid=mission
    original=copy.deepcopy(engine.scenario(sid))
    report=engine.break_plan(sid)
    roads=original['world']['roads']
    assert len(report['closures'])==len(roads)
    assert {c['road_id'] for c in report['closures']}=={r['id'] for r in roads}
    assert report['baseline']['success'] and report['baseline']['elapsed']==40
    assert report['critical']['success'] is False
    assert report['critical']['failure'] in ('deadline_missed','no_route')
    assert report['repair']['success'] is True
    assert report['repair']['origin']!=original['ambulances'][0]['mission']['target']
    assert report['repair']['origin']!=original['ambulances'][0]['mission']['origin']
    assert engine.scenario(sid)==original
    failed=engine.scenario(report['failed_scenario_id'])
    repaired=engine.scenario(report['repaired_scenario_id'])
    assert failed['blocked']==repaired['blocked']==[report['critical']['road_id']]
    assert failed is not repaired and failed['world'] is not repaired['world']
    assert failed['ambulances'][0]['mission']['success'] is False
    assert repaired['ambulances'][0]['mission']['success'] is True
    assert failed['clock']==failed['ambulances'][0]['mission']['elapsed']
    assert repaired['clock']==repaired['ambulances'][0]['mission']['elapsed']


def test_no_critical_road_is_reported_honestly(mission):
    engine,sid=mission
    engine.scenario(sid)['ambulances'][0]['mission']['deadline_seconds']=1000
    report=engine.break_plan(sid)
    assert report['critical'] is None
    assert report['failed_scenario_id'] is None
    assert report['repair'] is None
    assert len(engine.scenarios)==1


def test_analysis_comparison_leaves_unrelated_scenarios_untouched(mission):
    engine,sid=mission
    unrelated=engine.clone(engine.scenario(sid),'Unrelated experiment')
    engine.scenario(unrelated)['ambulances'][0]['mission']=None
    before=copy.deepcopy(engine.scenario(unrelated))
    report=engine.break_plan(sid)
    engine.apply(Operation(action='compare'),sid)
    assert engine.scenario(unrelated)==before
    for key in ('original_scenario_id','failed_scenario_id','repaired_scenario_id'):
        m=engine.scenario(report[key])['ambulances'][0]['mission']
        assert m['status']=='running' and m['elapsed']==0
    engine.tick(90)
    results=[engine.scenario(report[key])['ambulances'][0]['mission'] for key in
             ('original_scenario_id','failed_scenario_id','repaired_scenario_id')]
    assert [m['elapsed'] for m in results]==[40,90,25]
    assert [m['success'] for m in results]==[True,False,True]


def test_no_route_and_unrepairable_station_are_reported(mission):
    engine,sid=mission
    scenario=engine.scenario(sid)
    scenario['world']['roads']=[r for r in scenario['world']['roads'] if r['kind']=='main']
    report=engine.break_plan(sid)
    assert report['critical']['failure']=='no_route'
    assert report['critical']['elapsed'] is None
    assert report['repair']['success'] is False
    assert all(r['failure']=='no_route' for r in report['repair_candidates'])


def test_arrival_exactly_on_deadline_succeeds(mission):
    engine,sid=mission
    engine.apply(Operation(action='set_deadline',deadline_seconds=40),sid)
    engine.tick(40)
    m=engine.scenario(sid)['ambulances'][0]['mission']
    assert m['deadline_met'] and m['success']


def test_new_constraints_invalidate_old_analysis(mission):
    engine,sid=mission
    engine.break_plan(sid)
    engine.apply(Operation(action='restrict',road='main'),sid)
    assert engine.analysis is None


def test_already_failed_baseline_rejected_without_mutation(mission):
    engine,sid=mission
    engine.scenario(sid)['ambulances'][0]['mission']['deadline_seconds']=1
    before=engine.snapshot()
    with pytest.raises(ValueError,match='baseline'):
        engine.break_plan(sid)
    assert engine.snapshot()==before


def test_explanation_cannot_add_unverified_claims(mission,monkeypatch):
    from backend import explanations
    engine,sid=mission
    report=engine.break_plan(sid)
    monkeypatch.setattr(explanations,'choose_facts',lambda facts:['baseline','critical','repair'])
    result=explanations.explain(report)
    assert result['sentences']==[result['facts'][k] for k in ['baseline','critical','repair']]
    monkeypatch.setattr(explanations,'choose_facts',lambda facts:['invented'])
    with pytest.raises(ValueError,match='unverified'):
        explanations.explain(report)


def test_break_plan_voice_operation_and_api(mission,monkeypatch):
    from backend import main,integrations
    from fastapi.testclient import TestClient
    engine,sid=mission
    monkeypatch.setattr(main,'engine',engine)
    monkeypatch.setattr(integrations,'gemini_plan',lambda text,context:(Operation(action='break_plan',deadline_seconds=65), 'mock'))
    with TestClient(main.app) as client:
        r=client.post('/api/command',json={'text':'इस योजना को तोड़ो','mode':'gemini','scenario_id':sid})
        assert r.status_code==200
        assert len(r.json()['analysis']['closures'])==len(engine.scenario(sid)['world']['roads'])
        assert len(r.json()['scenarios'])==3


def test_voice_cyberwave_execution_reports_unverified(mission,monkeypatch):
    from backend import main,integrations
    from fastapi.testclient import TestClient
    engine,sid=mission
    monkeypatch.setattr(main,'engine',engine)
    monkeypatch.setattr(integrations,'gemini_plan',lambda text,context:(Operation(action='cyberwave_run'),'mock'))
    async def probe(): return {'configured':True,'verified':False,'status':'unverified','detail':'No simulator telemetry; no command sent.'}
    monkeypatch.setattr(integrations,'cyberwave_probe',probe)
    with TestClient(main.app) as client:
        r=client.post('/api/command',json={'text':'सिमुलेशन में रोबोट चलाओ','mode':'gemini','scenario_id':sid})
        assert r.status_code==200
        assert r.json()['cyberwave_result']['verified'] is False
        assert 'no command sent' in r.json()['cyberwave_result']['detail']
