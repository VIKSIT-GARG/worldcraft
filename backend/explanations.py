"""Gemini selects verified facts; it never writes numerical claims."""
import json
import os

from .integrations import GEMINI_CONFIG, IntegrationError


def verified_facts(report):
    baseline=report['baseline']; deadline=report['deadline_seconds']
    facts={
        'baseline': f"The original rescue arrived in {baseline['elapsed']:.1f} seconds against a {deadline:g}-second deadline.",
        'coverage': f"The simulation tested {len(report['closures'])} individual road closures.",
        'assumption': report['assumption'],
    }
    critical=report['critical']
    if critical is None:
        facts['critical']='No single tested road closure caused the rescue to miss its deadline.'
    elif critical['failure']=='no_route':
        facts['critical']=f"Closing road {critical['road_id']} left no route for the rescue."
    else:
        facts['critical']=f"Closing road {critical['road_id']} delayed the rescue to {critical['elapsed']:.1f} seconds, beyond the {deadline:g}-second deadline."
    repair=report['repair']
    if repair:
        if repair['success']:
            facts['repair']=f"Pre-staging the ambulance at {repair['station']} reached the destination in {repair['elapsed']:.1f} seconds under the same closure."
        elif repair['failure']=='no_route':
            facts['repair']='None of the tested alternative building stations produced a route under the critical closure.'
        else:
            facts['repair']=f"The best tested alternative station, {repair['station']}, still missed the deadline at {repair['elapsed']:.1f} seconds."
    elif critical:
        facts['repair']='No eligible alternative building station was available for a repositioning test.'
    return facts


def choose_facts(facts):
    """Ask Gemini to choose/order IDs from a closed set of measured statements."""
    import google.auth
    from google import genai
    from google.genai import types
    config={k:os.getenv(k,'').strip() for k in GEMINI_CONFIG}
    missing=[k for k,v in config.items() if not v]
    if missing: raise IntegrationError('gemini','Missing Gemini configuration: '+', '.join(missing),503)
    try:
        credentials,_=google.auth.default(scopes=['https://www.googleapis.com/auth/cloud-platform'])
        schema={'type':'object','properties':{'fact_ids':{'type':'array','items':{'type':'string','enum':list(facts)},'minItems':1,'maxItems':len(facts)}},'required':['fact_ids']}
        with genai.Client(vertexai=True,project=config['GOOGLE_CLOUD_PROJECT'],location=config['GOOGLE_CLOUD_LOCATION'],credentials=credentials,http_options=types.HttpOptions(api_version='v1',timeout=45000,retry_options=types.HttpRetryOptions(attempts=1))) as client:
            result=client.models.generate_content(model=config['GEMINI_MODEL_ID'],contents=json.dumps(facts,ensure_ascii=False),config=types.GenerateContentConfig(system_instruction='Select and order the most useful fact IDs to explain the simulated rescue. Include the baseline, critical result, repair if present, and the staging assumption. Do not create new facts.',response_mime_type='application/json',response_schema=schema,max_output_tokens=2048))
        if not result.candidates or result.candidates[0].finish_reason!=types.FinishReason.STOP: raise ValueError('Incomplete response')
        data=json.loads(result.text or '')
        if not isinstance(data,dict) or set(data)!={'fact_ids'} or not isinstance(data['fact_ids'],list): raise ValueError('Invalid selection')
        return data['fact_ids']
    except IntegrationError: raise
    except Exception as exc:
        raise IntegrationError('gemini','Gemini could not select verified simulation facts. The measured results remain available.',502) from exc


def explain(report):
    facts=verified_facts(report)
    selected=choose_facts(facts)
    if not selected or any(not isinstance(key,str) or key not in facts for key in selected) or len(set(selected))!=len(selected):
        raise ValueError('Gemini selected an unverified or duplicate fact.')
    return {'sentences':[facts[key] for key in selected],'facts':facts,'source':'Gemini-selected simulation facts'}
