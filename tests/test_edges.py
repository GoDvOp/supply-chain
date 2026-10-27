import json
import os
import subprocess
import sys
from copy import deepcopy

import pytest

from conftest import ROOT,direct_candidates
from supply_risk.engine import assess,scenario_by_id
from supply_risk.errors import DataError
from supply_risk.loader import read_json,seal
from supply_risk.reports import markdown,mermaid

@pytest.mark.integration
def test_real_cycle_and_search_limits(imported):
    db,d=imported
    d=deepcopy(d)
    d['version']='cycle'
    base=next(e for e in d['entities'] if e['id']=='a_hr_uses')
    a={**base,'id':'cycle_back','subject_id':'hrpro','predicate':'DEPENDS_ON','object_id':'lenta'}
    d['entities'].append(a)
    seal(d)
    db.import_dataset(d)
    c=db.candidates(d,scenario_by_id(d,'S1'),max_depth=3,max_paths=1)
    assert c['paths_limit_reached'] and c['depth_limit_reached'] and c['incomplete']
    assert all(len(p['nodes'])==len(set(p['nodes'])) for p in c['paths'])
    r=assess(d,'S1',c,'evidence',as_of='2026-10-26')
    assert any('Поиск неполон' in x for x in r['limitations'])

@pytest.mark.integration
def test_atomic_transaction_rollback(imported):
    db,d=imported
    d=deepcopy(d)
    d['version']='rollback'
    seal(d)
    def fail_after_import(tx):
        db._import(tx,d)
        raise DataError('Injected failure before commit')
    with db.driver.session() as session:
        with pytest.raises(DataError): session.execute_write(fail_after_import)
    with pytest.raises(DataError): db.load(d['dataset_id'],'rollback')


def test_conflict_and_outdated_notes(dataset):
    d=deepcopy(dataset)
    a=next(e for e in d['entities'] if e['id']=='a_provides')
    a['conflicts_with']=['a_orders_uses']
    a['as_of_date']='2020-01-01'
    seal(d)
    cfg=read_json(ROOT/'experiments/edi_base.json')
    r=assess(d,'S3',direct_candidates(d,'S3'),'experiment',cfg,as_of='2026-10-26')
    assert r['paths'][0]['status']=='conditional'
    assert any('Конфликт' in x for x in r['paths'][0]['limitations'])
    assert any('актуальности' in x for x in r['paths'][0]['limitations'])
    text=markdown(r)
    assert 'Конфликт' in text and 'актуальности' in text


def test_evidence_never_uses_hypothetical_threshold(dataset):
    d=deepcopy(dataset)
    p=next(e for e in d['entities'] if e['id']=='orders')
    p['threshold']={'metric':'total_time','first':8,'second':11,'origin':'проектная гипотеза'}
    p['threshold_origin']='assumption'
    seal(d)
    r=assess(d,'S3',direct_candidates(d,'S3'),'evidence',as_of='2026-10-26')
    assert r['calculations']['orders']['severity']=='unknown_severity'


def test_empty_path_wording(dataset):
    c=direct_candidates(dataset,'S3')
    c['paths']=[]
    r=assess(dataset,'S3',c,'evidence')
    assert 'Путь не найден в данном наборе' in markdown(r)
    assert 'Риск отсутствует' not in markdown(r)


def test_cli_error_codes(tmp_path):
    env={**os.environ,'NEO4J_PASSWORD':'local-test-only','NEO4J_URI':'bolt://127.0.0.1:1'}
    result=subprocess.run([sys.executable,'-m','supply_risk.cli','list','--dataset','lenta','--version','1','--kind','scenarios'],env=env,capture_output=True,text=True)
    assert result.returncode==3 and 'Ошибка инфраструктуры' in result.stderr and 'Traceback' not in result.stderr
    bad=tmp_path/'bad.json'
    bad.write_text('{bad')
    result=subprocess.run([sys.executable,'-m','supply_risk.cli','validate',str(bad)],capture_output=True,text=True)
    assert result.returncode==2 and 'Ошибка данных' in result.stderr


def test_no_json_nan_and_sensitivity_render(dataset):
    from supply_risk.sensitivity import sensitivity
    r=sensitivity(read_json(ROOT/'experiments/grid.json'))
    assert 'Infinity' not in json.dumps(r,allow_nan=False)
    assert markdown(r).count('| non_draining |')==12
    assert mermaid(dataset).startswith('flowchart LR')


def test_unknown_priority_multi_condition(dataset):
    r=assess(dataset,'S2',direct_candidates(dataset,'S2'),'evidence',as_of='2026-10-26')
    assert len(r['clarification_priorities'])==4
    assert all(p['limitations'] for p in r['clarification_priorities'])
    # Toggling one condition still explains a blocked versus conditional path.
    assert all(p['priority']=='P2' for p in r['clarification_priorities'])


def test_counterfactual_preserves_evidence_assertion_status(dataset):
    d=deepcopy(dataset)
    next(e for e in d['entities'] if e['id']=='a_provides')['status']='assumption'
    seal(d)
    r=assess(d,'S3',direct_candidates(d,'S3'),'evidence',as_of='2026-10-26')
    priority=r['clarification_priorities'][0]
    assert priority['true_result']['paths'][0]['status']=='conditional'
    assert priority['false_result']['paths'][0]['status']=='blocked'


def test_parameter_evidence_provenance_is_not_reused_for_override(dataset):
    d=deepcopy(dataset)
    d['parameters']['lambda']={'value':25,'origin':'source_supported','evidence_ids':['src_edi']}
    seal(d)
    cfg=read_json(ROOT/'experiments/edi_base.json')
    r=assess(d,'S3',direct_candidates(d,'S3'),'experiment',cfg,as_of='2026-10-26')
    assert r['parameter_provenance']['lambda']['origin']=='experiment'
    assert r['parameter_provenance']['lambda']['evidence_ids']==[]
    assert r['parameter_provenance']['lambda']['base_evidence_ids']==['src_edi']
