"""Deterministic kinematic city simulation. Coordinates and distances are metres."""
import copy
import math
import uuid
import networkx as nx
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal

class Operation(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: Literal['create_city','dispatch','restrict','unrestrict','clone','run','compare','break_plan','set_deadline','explain','cyberwave_run','robot_prepare','robot_sync','robot_execute','robot_stop','robot_create','robot_forward','robot_observe','robot_catalog','robot_design']
    buildings: list[str] = Field(default_factory=lambda: ['A','B','C','D'], min_length=1, max_length=12)
    ambulances: int = Field(default=2, ge=1, le=4)
    destination: str = Field(default='A', max_length=30, description='Arrival building ID for dispatch, copied from the instruction (for example C for Tower C). Never put a dispatch target in name.')
    origin: str = Field(default='hospital', max_length=30)
    ambulance: str = Field(default='AMB-01', max_length=20)
    road: str = Field(default='main', max_length=40)
    name: str = Field(default='Unrestricted', max_length=50, description='New scenario name for clone only. This is never a dispatch destination.')
    deadline_seconds: float | None = Field(default=None, gt=0, le=86400, allow_inf_nan=False)
    robot_id:str|None=Field(default=None,max_length=80)
    robot_description:str=Field(default='',max_length=1000)
    robot_speed:float=Field(default=.24,ge=0,le=.48,allow_inf_nan=False)
    robot_duration:float=Field(default=4,ge=.2,le=10,allow_inf_nan=False)

class Engine:
    def __init__(self):
        self.scenarios = {}
        self.events = []
        self.analysis = None

    def event(self, text):
        self.events = ([text] + self.events)[:30]

    def create(self, names, count):
        names = [n.strip().upper() for n in names]
        if len(set(names)) != len(names) or any(not n or len(n)>20 or n=='HOSPITAL' for n in names):
            raise ValueError('Building names must be unique, nonempty and at most 20 characters; hospital is reserved.')
        nodes = {f'{x}:{z}': [x,z] for x in range(-240,241,120) for z in [-120,0,120]}
        roads = []
        for key, (x,z) in nodes.items():
            for xx,zz in [(x+120,z),(x,z+120)]:
                other = f'{xx}:{zz}'
                if other in nodes:
                    roads.append({'id': f'{key}/{other}', 'a':key,'b':other,'kind':'main' if z==zz==0 else 'local','length':120.,'speed':12. if z==zz==0 else 8.})
        sites = [(240,0),(0,-120),(120,120),(-120,-120),(240,-120),(240,120),(0,120),(-120,120),(120,-120),(-240,-120),(-240,120),(120,0)]
        buildings = [{'id':'hospital','label':'Hospital','node':'-240:0','x':-240,'z':-32,'height':22,'kind':'hospital'}]
        for i,n in enumerate(names):
            x,z = sites[i]
            buildings.append({'id':n,'label':f'Tower {n}','node':f'{x}:{z}','x':x,'z':z-32,'height':35+(i*17)%60,'kind':'tower'})
        world = {'nodes':nodes,'roads':roads,'buildings':buildings}
        sid = uuid.uuid4().hex[:8]
        self.scenarios = {sid: {'id':sid,'name':'Original city','world':world,'blocked':[], 'clock':0.,'ambulances':[{'id':f'AMB-{i+1:02}','position':[-240.,0.], 'mission':None} for i in range(count)]}}
        self.analysis = None
        self.event(f'Generated {len(names)} towers, one hospital and {count} ambulances.')
        return sid

    def scenario(self, sid):
        if sid not in self.scenarios: raise ValueError('Scenario not found. Create a city first.')
        return self.scenarios[sid]

    def graph(self, s):
        g = nx.Graph()
        for n,p in s['world']['nodes'].items(): g.add_node(n,position=p)
        for r in s['world']['roads']:
            if r['kind'] not in s['blocked'] and r['id'] not in s['blocked']:
                g.add_edge(r['a'],r['b'],**r)
        return g

    def site(self,s,name):
        normalized = name.lower().replace('tower','').replace('building','').strip()
        for b in s['world']['buildings']:
            if b['id'].lower() == normalized: return b['node']
        raise ValueError(f'Unknown location: {name}')

    def vehicle(self,s,aid):
        for a in s['ambulances']:
            if a['id']==aid: return a
        raise ValueError('Unknown ambulance.')

    def plan(self,s,position,target):
        g = self.graph(s)
        nodes = s['world']['nodes']
        start = next((n for n,p in nodes.items() if math.dist(p,position)<1e-6), None)
        # Mid-edge constraints allow only the short egress from an occupied edge.
        egress = False
        if start is None:
            start = '__current__'
            g.add_node(start,position=position)
            for r in s['world']['roads']:
                a,b = nodes[r['a']],nodes[r['b']]
                if abs(math.dist(a,position)+math.dist(position,b)-r['length'])<1e-5:
                    blocked = r['kind'] in s['blocked'] or r['id'] in s['blocked']
                    egress = blocked
                    for end in [r['a'],r['b']]:
                        g.add_edge(start,end,length=math.dist(position,nodes[end]),speed=r['speed'],kind='egress' if blocked else r['kind'],id=r['id'])
                    break
        try:
            path = nx.astar_path(g,start,target,heuristic=lambda a,b:math.dist(g.nodes[a]['position'],g.nodes[b]['position']),weight='length')
        except (nx.NetworkXNoPath,nx.NodeNotFound):
            return {'status':'no_route','points':[list(position)],'segments':[],'distance':None,'travel_time':None,'egress':False}
        segments = [{'length':g[a][b]['length'],'speed':g[a][b]['speed'],'kind':g[a][b]['kind'],'road':g[a][b]['id']} for a,b in zip(path,path[1:])]
        return {'status':'ready','points':[list(g.nodes[n]['position']) for n in path],'segments':segments,'distance':sum(x['length'] for x in segments),'travel_time':sum(x['length']/x['speed'] for x in segments),'egress':egress}

    def dispatch(self,s,op):
        a = self.vehicle(s,op.ambulance)
        start,target = self.site(s,op.origin),self.site(s,op.destination)
        if a['mission'] and a['mission']['status']=='running': raise ValueError('Ambulance has an active mission. Reset its scenario before issuing a new mission.')
        # Dispatch is staged at the named origin, explicitly reflected in UI/log.
        a['position'] = list(s['world']['nodes'][start])
        route = self.plan(s,a['position'],target)
        a['mission'] = {**route,'origin':start,'target':target,'destination':op.destination,'elapsed':0.,'travelled':0.,'cursor':0.,'started':route['status']=='ready','success':False,'deadline_seconds':op.deadline_seconds,'deadline_met':None}
        if route['status']=='ready': a['mission']['status']='running'
        self.event(f"{a['id']} staged at {op.origin}; route to {op.destination}: {route['status']}.")

    def restrict(self,s,road,blocked):
        if road not in ['main','local']+[r['id'] for r in s['world']['roads']]: raise ValueError('Unknown road constraint.')
        if blocked and road not in s['blocked']: s['blocked'].append(road)
        if not blocked and road in s['blocked']: s['blocked'].remove(road)
        for a in s['ambulances']:
            m = a['mission']
            if m and m['status']!='arrived':
                running = m['status']=='running' or m.get('started',False)
                route = self.plan(s,a['position'],m['target'])
                m.update(route)
                m['cursor']=0.
                if route['status']=='ready' and running: m['status']='running'
        self.event(f"{s['name']}: {'blocked' if blocked else 'opened'} {road}; recomputed routes from current positions.")

    def clone(self,s,name):
        if len(self.scenarios)>=6: raise ValueError('Maximum six scenarios; reset city to start over.')
        new = copy.deepcopy(s)
        new['id']=uuid.uuid4().hex[:8]; new['name']=name
        self.scenarios[new['id']]=new
        self.event(f'Cloned {s["name"]} into independent scenario {name}.')
        return new['id']

    def reset_missions(self,s):
        s['clock']=0.
        for a in s['ambulances']:
            m=a['mission']
            if m:
                a['position']=list(s['world']['nodes'][m['origin']])
                route=self.plan(s,a['position'],m['target'])
                a['mission']={**m,**route,'elapsed':0.,'travelled':0.,'cursor':0.,'started':False,'success':False,'deadline_met':None}

    def run(self,s):
        if not any(a['mission'] for a in s['ambulances']): raise ValueError('Dispatch an ambulance first.')
        for a in s['ambulances']:
            if a['mission'] and a['mission']['status']=='ready':
                a['mission']['status']='running'; a['mission']['started']=True

    def tick(self,dt):
        for s in self.scenarios.values():
            if any(a['mission'] and a['mission']['status']=='running' for a in s['ambulances']): s['clock']+=dt
            for a in s['ambulances']:
                m=a['mission']
                if not m or m['status']!='running': continue
                remaining=dt
                while remaining>1e-9 and m['cursor'] < m['distance']-1e-8:
                    consumed=0.
                    for i,seg in enumerate(m['segments']):
                        if m['cursor'] < consumed+seg['length']-1e-8:
                            advance=min(seg['length']-(m['cursor']-consumed),remaining*seg['speed'])
                            spent=advance/seg['speed']; remaining-=spent
                            m['elapsed']+=spent; m['travelled']+=advance; m['cursor']+=advance
                            t=(m['cursor']-consumed)/seg['length']
                            p,q=m['points'][i:i+2]
                            a['position']=[p[j]+(q[j]-p[j])*t for j in [0,1]]
                            break
                        consumed+=seg['length']
                    else: break
                if m['cursor'] >= m['distance']-1e-8:
                    deadline=m.get('deadline_seconds')
                    m['status']='arrived';m['deadline_met']=None if deadline is None else m['elapsed']<=deadline+1e-8
                    m['success']=m['deadline_met'] is not False;a['position']=m['points'][-1]
                elif m.get('deadline_seconds') is not None and m['elapsed']>m['deadline_seconds']:
                    m['deadline_met']=False

    def break_plan(self, sid, ambulance='AMB-01', deadline=None):
        """Run a fresh baseline, every one-road closure, and station alternatives.

        Repositioning is pre-staging at an existing non-destination building;
        its relocation time is deliberately excluded from the rescue clock.
        Trials are isolated copies and do not advance the user's live scenario.
        """
        source=self.scenario(sid)
        original=self.vehicle(source,ambulance)
        mission=original['mission']
        if not mission: raise ValueError('Dispatch the selected ambulance before testing the plan.')
        deadline=deadline if deadline is not None else mission.get('deadline_seconds')
        if deadline is None: raise ValueError('Set a rescue deadline in seconds before testing the plan.')

        def trial(road=None, origin=None):
            isolated=Engine(); sc=copy.deepcopy(source)
            isolated.scenarios={sc['id']:sc}
            vehicle=isolated.vehicle(sc,ambulance); m=vehicle['mission']
            m['deadline_seconds']=deadline
            if origin is not None: m['origin']=origin
            if road is not None and road not in sc['blocked']: sc['blocked'].append(road)
            isolated.reset_missions(sc); isolated.run(sc)
            duration=max((a['mission']['travel_time'] for a in sc['ambulances'] if a['mission'] and a['mission']['status']=='running'),default=0.)
            isolated.tick(duration)
            m=vehicle['mission']
            failure='no_route' if m['status']=='no_route' else 'deadline_missed' if not m['success'] else None
            return sc,{'success':m['success'],'failure':failure,'distance':m['distance'],
                       'elapsed':m['elapsed'] if m['status']=='arrived' else None,
                       'deadline_seconds':deadline,'origin':m['origin'],'destination':m['target']}

        _,baseline=trial()
        if not baseline['success']:
            raise ValueError('The baseline already fails the deadline or has no route; choose a feasible baseline first.')
        closures=[]
        for road in source['world']['roads']:
            _,result=trial(road['id'])
            closures.append({'road_id':road['id'],**result})
        failures=[r for r in closures if not r['success']]
        critical=sorted(failures,key=lambda r:(r['failure']!='no_route',-(r['elapsed'] or 0),r['road_id']))[0] if failures else None
        report={'baseline':baseline,'deadline_seconds':deadline,'closures':closures,'critical':critical,
                'original_scenario_id':sid,'failed_scenario_id':None,'repaired_scenario_id':None,
                'repair':None,'repair_candidates':[],
                'assumption':'Ambulance is pre-staged at an existing building before the rescue; relocation time is excluded.'}
        if critical:
            if len(self.scenarios)>4: raise ValueError('Two scenario slots are needed for failure and repair; create a new city first.')
            candidates=[]
            for building in source['world']['buildings']:
                node=building['node']
                if node in (mission['origin'],mission['target']): continue
                sc,result=trial(critical['road_id'],node)
                result['station']=building['label']
                candidates.append((sc,result))
            report['repair_candidates']=[r for _,r in candidates]
            if candidates:
                candidates.sort(key=lambda pair:(not pair[1]['success'],pair[1]['failure']=='no_route',pair[1]['elapsed'] if pair[1]['elapsed'] is not None else float('inf'),pair[1]['origin']))
                repair_scenario,repair=candidates[0]
                report['repair']=repair
            else:
                repair_scenario=None
            failed,_=trial(critical['road_id'])
            failed['id']=uuid.uuid4().hex[:8];failed['name']='Critical closure'
            report['failed_scenario_id']=failed['id']
            self.scenarios[failed['id']]=failed
            if repair_scenario:
                repair_scenario['id']=uuid.uuid4().hex[:8];repair_scenario['name']='Repositioned ambulance'
                report['repaired_scenario_id']=repair_scenario['id']
                self.scenarios[repair_scenario['id']]=repair_scenario
        if mission.get('deadline_seconds') != deadline:
            mission['deadline_seconds']=deadline
            if mission['status']=='arrived':
                mission['deadline_met']=mission['elapsed']<=deadline+1e-8
                mission['success']=mission['deadline_met']
        self.analysis=report
        self.event(f"Tested {len(closures)} individual road closures against a {deadline:g}s deadline.")
        return copy.deepcopy(report)

    def apply(self,op,sid=None):
        if op.action=='create_city': return self.create(op.buildings,op.ambulances)
        if op.action in ('explain','cyberwave_run') or op.action.startswith('robot_'): raise ValueError('This operation requires the command endpoint.')
        s=self.scenario(sid)
        if op.action=='dispatch': self.dispatch(s,op)
        elif op.action in ['restrict','unrestrict']: self.restrict(s,op.road,op.action=='restrict')
        elif op.action=='clone': return self.clone(s,op.name)
        elif op.action=='run': self.run(s)
        elif op.action=='set_deadline':
            m=self.vehicle(s,op.ambulance)['mission']
            if not m: raise ValueError('Dispatch the selected ambulance first.')
            if op.deadline_seconds is None: raise ValueError('Provide a rescue deadline in seconds.')
            m['deadline_seconds']=op.deadline_seconds
            if m['status']=='arrived':
                m['deadline_met']=m['elapsed']<=op.deadline_seconds+1e-8
                m['success']=m['deadline_met']
        elif op.action=='break_plan': self.break_plan(sid,op.ambulance,op.deadline_seconds)
        elif op.action=='compare':
            scenarios=list(self.scenarios.values())
            if self.analysis:
                ids=[self.analysis[k] for k in ('original_scenario_id','failed_scenario_id','repaired_scenario_id') if self.analysis.get(k)]
                scenarios=[self.scenario(i) for i in ids]
            if len(scenarios)<2: raise ValueError('Comparison needs at least two scenarios; this analysis may have no failing closure.')
            if any(not any(a['mission'] for a in sc['ambulances']) for sc in scenarios): raise ValueError('Every compared scenario needs a dispatched mission.')
            for sc in scenarios: self.reset_missions(sc);self.run(sc)
            self.event('Comparison started from each scenario’s mission origin; each scenario runs independently.')
        if op.action in ('dispatch','restrict','unrestrict','clone','set_deadline'):
            self.analysis=None
        return sid

    def snapshot(self):
        return copy.deepcopy({'scenarios':list(self.scenarios.values()),'events':self.events,'analysis':self.analysis})
