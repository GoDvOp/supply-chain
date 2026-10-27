from copy import deepcopy
import math

import pytest

from supply_risk.compare import compare
from supply_risk.engine import assess,scenario_by_id,signature
from supply_risk.errors import DataError
from supply_risk.loader import load,read_json,seal,validate
from supply_risk.queue import calculate
from supply_risk.sensitivity import sensitivity
from conftest import ROOT,direct_candidates


def assessment(d,s='S3',mode='experiment',config=None):
    return assess(d,s,direct_candidates(d,s),mode,config,as_of='2026-10-26')

@pytest.mark.integration
def test_T01_idempotent_import(imported):
    db,d=imported
    with db.driver.session() as session:
        before=session.run('MATCH (n {dataset_id:$ds}) OPTIONAL MATCH (n)-[r]->() RETURN count(DISTINCT n) AS n,count(r) AS r',ds=d['dataset_id']).single().data()
    result=db.import_dataset(d)
    assert result['created_nodes']==result['created_relationships']==0
    with db.driver.session() as session:
        after=session.run('MATCH (n {dataset_id:$ds}) OPTIONAL MATCH (n)-[r]->() RETURN count(DISTINCT n) AS n,count(r) AS r',ds=d['dataset_id']).single().data()
    assert before==after
    assert db.load(d['dataset_id'],'1')==d

@pytest.mark.integration
def test_T02_bad_reference_no_partial_write(imported):
    db,d=imported
    invalid=deepcopy(d)
    invalid['version']='bad'
    next(e for e in invalid['entities'] if e['label']=='Assertion')['object_id']='missing'
    seal(invalid)
    with pytest.raises(DataError): db.import_dataset(invalid)
    with pytest.raises(DataError): db.load(d['dataset_id'],'bad')
    assert db.load(d['dataset_id'],'1')==d

@pytest.mark.parametrize('value,status',[('unknown','conditional'),(False,'blocked'),(True,'applicable_in_scenario')])
def test_T03_three_valued_access(dataset,value,status):
    config=read_json(ROOT/'experiments/access_base.json')
    config['conditions']['active_access']['value']=value
    r=assessment(dataset,'S2',config=config)
    assert r['paths'][0]['status']==status
    assert r['paths'][0]['conditions'][0]['origin']=='experiment'


def test_T04_implemented_does_not_imply_access(dataset):
    r=assessment(dataset,'S2',mode='evidence')
    assert r['paths'][0]['status']=='conditional'
    assert any(c['id']=='active_access' and c['value']=='unknown' for c in r['unknowns'])


def test_T05_base_edi():
    r=calculate({'lambda':100,'T':8,'mu_b':0,'mu_r':300})
    assert (r['Q'],r['T_clear'],r['total_time'])==(800,4,12)


def test_T06_bypass():
    r=calculate({'lambda':100,'T':8,'mu_b':40,'mu_r':300})
    assert (r['Q'],r['T_clear'],r['total_time'])==(480,2.4,10.4)

@pytest.mark.parametrize('recovery,behavior',[(100,'constant'),(80,'growing')])
def test_T07_non_draining(recovery,behavior):
    r=calculate({'lambda':100,'T':8,'mu_b':0,'mu_r':recovery})
    assert r['status']=='non_draining' and r['T_clear'] is None and r['total_time'] is None
    assert r['queue_behavior']==behavior

@pytest.mark.parametrize('duration,bypass',[(0,0),(8,100),(8,200)])
def test_T08_no_backlog(duration,bypass):
    r=calculate({'lambda':100,'T':duration,'mu_b':bypass,'mu_r':0})
    assert (r['Q'],r['T_clear'])==(0,0)

@pytest.mark.parametrize('value',[-1,'100',True,float('inf'),float('nan')])
def test_T09_invalid_numbers(value):
    with pytest.raises(DataError): calculate({'lambda':value,'T':8,'mu_b':0,'mu_r':300})

@pytest.mark.integration
def test_T10_two_paths_one_process(db,synthetic):
    d=deepcopy(synthetic)
    d['dataset_id']='test_two_paths'
    for e in d['entities']: e['dataset_id']=d['dataset_id']
    seal(d)
    try:
        db.import_dataset(d)
        c=db.candidates(d,scenario_by_id(d,'common'))
        r=assess(d,'common',c,'experiment',read_json(ROOT/'experiments/synthetic.json'))
        assert len(r['paths'])==2
        assert len(r['affected_processes'])==len(r['calculations'])==1
        assert r['affected_processes'][0]['path_count']==2
    finally:
        with db.driver.session() as session:
            session.run('MATCH (n {dataset_id:$ds}) DETACH DELETE n',ds=d['dataset_id']).consume()

@pytest.mark.integration
def test_T11_cycle_and_limits(imported):
    db,d=imported
    # Ownership produces an undirected cycle; no repeated nodes are returned.
    c=db.candidates(d,scenario_by_id(d,'S3'),max_depth=1)
    assert c['incomplete'] and c['depth_limit_reached']
    assert not c['paths']
    c=db.candidates(d,scenario_by_id(d,'S3'),max_depth=5,max_paths=1)
    for p in c['paths']:
        assert len(p['nodes'])==len(set(p['nodes']))
    r=assess(d,'S3',c,'evidence')
    if c['incomplete']: assert any('Поиск неполон' in s for s in r['limitations'])


def test_T12_shared_infrastructure(synthetic):
    local=assessment(synthetic,'local',config=read_json(ROOT/'experiments/synthetic.json'))
    common=assessment(synthetic,'common',config=read_json(ROOT/'experiments/synthetic.json'))
    p=local['affected_processes'][0]
    assert p['dependency_evaluation']['fully_unavailable'] is False
    assert p['dependency_evaluation']['state']=='degraded'
    assert not p['impact_confirmed_in_scenario']
    assert common['affected_processes'][0]['dependency_evaluation']['fully_unavailable'] is True
    assert common['affected_processes'][0]['impact_confirmed_in_scenario']


def test_T13_evidence_and_experiment(dataset,example_config):
    original=deepcopy(dataset)
    evidence=assessment(dataset,mode='evidence')
    experiment=assessment(dataset,config=example_config)
    assert evidence['calculations']['orders']['status']=='insufficient_data'
    assert experiment['calculations']['orders']['Q']==800
    assert len(experiment['overrides'])==6
    assert experiment['affected_processes'][0]['depends_on_assumptions']
    assert dataset==original
    with pytest.raises(DataError): assessment(dataset,mode='evidence',config=example_config)


def test_T14_unknown_priority(dataset,example_config):
    cfg=deepcopy(example_config)
    del cfg['conditions']['flow_affected']
    r=assessment(dataset,config=cfg)
    priority=r['clarification_priorities'][0]
    assert priority['priority']=='P1'
    assert priority['true_result']['paths'][0]['status']=='applicable_in_scenario'
    assert priority['false_result']['paths'][0]['status']=='blocked'
    assert assessment(dataset,config=cfg)['clarification_priorities']==r['clarification_priorities']
    assert assessment(dataset,mode='evidence')['clarification_priorities'][0]['true_result']['calculations']['orders']['status']=='insufficient_data'


def test_T15_all_grid_rows():
    grid=read_json(ROOT/'experiments/grid.json')
    r=sensitivity(grid)
    assert r['combinations']==len(r['rows'])==27
    assert r['non_draining_count']==12
    finite=[x['total_time'] for x in r['rows'] if x['total_time'] is not None]
    assert r['finite_mean']==sum(finite)/len(finite)
    grid['max_combinations']=26
    with pytest.raises(DataError): sensitivity(grid)

@pytest.mark.integration
def test_T16_isolation(imported,db,synthetic):
    _,d=imported
    s=deepcopy(synthetic)
    s['dataset_id']='test_isolation'
    for e in s['entities']: e['dataset_id']=s['dataset_id']
    seal(s)
    try:
        db.import_dataset(s)
        r=assess(d,'S3',db.candidates(d,scenario_by_id(d,'S3')),'evidence')
        assert all(n['id'] not in {'shared_service','main_service','delivery'} for p in r['paths'] for n in p['nodes'])
        assert r['dataset_id']==d['dataset_id']
    finally:
        with db.driver.session() as session:
            session.run('MATCH (n {dataset_id:$ds}) DETACH DELETE n',ds=s['dataset_id']).consume()

@pytest.mark.integration
def test_T17_export_and_restore(imported,example_config,tmp_path):
    db,d=imported
    exported=db.load(d['dataset_id'],'1')
    import json
    path=tmp_path/'export.json'
    path.write_text(json.dumps(exported,ensure_ascii=False))
    restored=load(path)
    before=assess(d,'S3',db.candidates(d,scenario_by_id(d,'S3')),'experiment',example_config,as_of='2026-10-26')
    with db.driver.session() as session:
        session.run('MATCH (n {dataset_id:$ds}) DETACH DELETE n',ds=d['dataset_id']).consume()
    db.import_dataset(restored)
    after=assess(restored,'S3',db.candidates(restored,scenario_by_id(restored,'S3')),'experiment',example_config,as_of='2026-10-26')
    assert signature(before)==signature(after)
    assert before['input_hash']==after['input_hash']


def test_T18_unknown_threshold():
    r=calculate({'lambda':100,'T':8,'mu_b':0,'mu_r':300})
    assert r['Q']==800 and r['severity']=='unknown_severity' and r['threshold_exceeded'] is None

@pytest.mark.integration
def test_version_immutable_and_old_snapshot(imported):
    db,d=imported
    changed=deepcopy(d)
    next(e for e in changed['entities'] if e['label']=='Organization')['name']='New name'
    seal(changed)
    with pytest.raises(DataError): db.import_dataset(changed)
    changed['version']='2'
    seal(changed)
    db.import_dataset(changed)
    assert db.load(d['dataset_id'],'1')==d
    assert db.load(d['dataset_id'],'2')==changed
    for v in (d,changed):
        assert db.candidates(v,scenario_by_id(v,'S3'))['paths']


def test_compare_measures(dataset,example_config):
    r=compare(dataset,'S3',direct_candidates(dataset,'S3'),example_config,read_json(ROOT/'experiments/edi_bypass.json'))
    assert r['before']['calculations']['orders']['severity']=='severe'
    assert r['after']['calculations']['orders']['severity']=='substantial'
    assert r['changes'][0]['field']=='parameters.mu_b.value'
    for before,after,scenario in [('access_base','access_blocked','S2'),('access_base','access_limited','S2'),('update_base','update_blocked','S1')]:
        r=compare(dataset,scenario,direct_candidates(dataset,scenario),read_json(ROOT/f'experiments/{before}.json'),read_json(ROOT/f'experiments/{after}.json'))
        assert r['before']['paths'][0]['status']=='applicable_in_scenario'
        assert r['after']['paths'][0]['status']=='blocked'

@pytest.mark.parametrize('scenario',['S1','S2','S3','S4'])
def test_four_evidence_scenarios(dataset,scenario):
    r=assessment(dataset,scenario,mode='evidence')
    assert r['paths'] and all(p['status']=='conditional' for p in r['paths'])
    assert all(p['severity']=='unknown_severity' for p in r['affected_processes'])


def test_schema_hash_and_unknown_ids(dataset,example_config):
    d=deepcopy(dataset)
    d['entities'][0]['unexpected']=True
    with pytest.raises(DataError): validate(seal(d))
    d=deepcopy(dataset)
    d['title']='Tampered'
    with pytest.raises(DataError): validate(d)
    example_config['conditions']['unknown_id']={'value':True,'explanation':'test'}
    with pytest.raises(DataError): assessment(dataset,config=example_config)


def test_threshold_metric_and_limits():
    params={'lambda':100,'T':8,'mu_b':0,'mu_r':300}
    assert calculate(params,{'metric':'T','first':8,'second':11,'origin':'test'})['severity']=='limited'
    assert calculate(params,{'metric':'total_time','first':8,'second':11,'origin':'test'})['severity']=='severe'
    with pytest.raises(DataError): calculate(params,{'metric':'T','first':11,'second':8,'origin':'test'})
