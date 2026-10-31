"""Existing twin, simulation-only control and simulator-originated telemetry evidence.
Run in a bounded subprocess: python -m backend.cyberwave_probe.
No twin creation, editor pose manipulation, hardware command, or billable sim startup.
"""
import json
import math
import os
import threading
import time


def read_sample(data,now):
    if not isinstance(data,dict) or data.get('source_type')!='sim': return None
    p=data.get('position',data)
    try:
        xyz=[float(p[k]) for k in ['x','y','z']]
        stamp=float(data['timestamp'])
        if not all(math.isfinite(v) for v in xyz) or not math.isfinite(stamp) or abs(stamp-now)>10: return None
        return {'position':xyz,'timestamp':stamp,'source_type':'sim','received_at':now}
    except (KeyError,TypeError,ValueError): return None


def probe():
    report={'provider':'cyberwave','sdk':'0.7.4','mode':'playground','authenticated':False,'command_sent':False,'state_observed':False,'motion_observed':False,'verified':False,'samples':[]}
    if not os.getenv('CYBERWAVE_API_KEY') or not os.getenv('CYBERWAVE_TWIN_ID'):
        return {**report,'status':'blocked','detail':'Missing CYBERWAVE_API_KEY or CYBERWAVE_TWIN_ID.'}
    cw=None;robot=None;sent=False
    sample_ready=threading.Event();samples=[];guard=threading.Lock()
    try:
        from cyberwave import Cyberwave
        cw=Cyberwave(api_key=os.environ['CYBERWAVE_API_KEY'],mode='playground',environment_id=os.getenv('CYBERWAVE_ENVIRONMENT_ID'),timeout=8)
        cw.affect('playground')  # simulation routing, NO auto-started billable MuJoCo
        robot=cw.twin(twin_id=os.environ['CYBERWAVE_TWIN_ID'])
        report['authenticated']=True
        report['twin_id']=str(robot.uuid)
        if not callable(getattr(robot,'move_forward',None)):
            return {**report,'status':'unsupported','detail':'Selected twin has no supported locomotion move_forward method.'}
        def observe(data):
            sample=read_sample(data,time.time())
            if sample:
                with guard: samples.append(sample)
                sample_ready.set()
        robot.subscribe_position(observe)
        if not sample_ready.wait(8):
            return {**report,'status':'unverified','detail':'Authenticated; no fresh sim position telemetry. Open an active Cyberwave Playground for this twin. No movement command sent.'}
        with guard: before=samples[-1]
        report['state_observed']=True
        submitted_at=time.time()
        sent=True
        robot.move_forward(0.3,source_type='sim_tele')
        report['command_sent']=True
        # Bounded burst. Stop is sent even if command/telemetry subsequently fails.
        time.sleep(0.7)
        robot.move_forward(0.,source_type='sim_tele');sent=False
        deadline=time.monotonic()+6
        while time.monotonic()<deadline:
            with guard:
                changed=[p for p in samples if p['timestamp']>=submitted_at and math.dist(before['position'],p['position'])>0.005]
            if changed:
                report['motion_observed']=True;break
            time.sleep(0.1)
        with guard: report['samples']=samples[-12:]
        report['verified']=report['motion_observed']
        report['status']='verified' if report['verified'] else 'unverified'
        report['detail']='Fresh simulator telemetry changed after sim_tele command; stop sent. This is a supported twin probe, not city route execution.' if report['verified'] else 'Command submitted and stopped, but movement not observed; do not claim successful execution.'
    except Exception as e:
        report.update(status='failed',detail=f'Cyberwave SDK failed ({type(e).__name__}). Check token, twin access, MQTT network and Playground runtime.')
    finally:
        if sent and robot:
            try: robot.move_forward(0.,source_type='sim_tele')
            except Exception: report['stop_failed']=True
        if cw:
            try: cw.disconnect()
            except Exception: pass
    return report

if __name__=='__main__': print(json.dumps(probe()))
