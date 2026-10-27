import os
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import pytest

from supply_risk.loader import load, seal
from supply_risk.store import connect

ROOT=Path(__file__).resolve().parents[1]

@pytest.fixture
def dataset():
    return load(ROOT/'data/lenta.v1.json')

@pytest.fixture
def synthetic():
    return load(ROOT/'data/synthetic.v1.json')

@pytest.fixture
def db():
    if os.environ.get('RUN_NEO4J_TESTS') != '1':
        pytest.skip('Set RUN_NEO4J_TESTS=1 for the live database tests')
    env=ROOT/'.env'
    if env.exists():
        for line in env.read_text().splitlines():
            if '=' in line:
                key,value=line.split('=',1)
                os.environ.setdefault(key,value)
    with connect() as store:
        yield store

@pytest.fixture
def imported(db,dataset):
    d=deepcopy(dataset)
    d['dataset_id']='test_'+uuid4().hex
    for e in d['entities']:
        e['dataset_id']=d['dataset_id']
    seal(d)
    db.import_dataset(d)
    yield db,d
    with db.driver.session() as session:
        session.run('MATCH (n {dataset_id:$ds}) DETACH DELETE n',ds=d['dataset_id']).consume()

@pytest.fixture
def example_config():
    from supply_risk.loader import read_json
    return read_json(ROOT/'experiments/edi_base.json')


def direct_candidates(data,scenario_id):
    from supply_risk.engine import scenario_by_id
    sc=scenario_by_id(data,scenario_id)
    rules=[r for r in data['rules'] if r['id'] in sc['rule_ids']]
    graph={}
    for e in data['entities']:
        if e['label']!='Assertion': continue
        for r in rules:
            if e['id'] not in r['assertion_ids']: continue
            sub,obj=e['subject_id'],e['object_id']
            start,end=(sub,obj) if r['direction']=='forward' else (obj,sub)
            graph.setdefault(start,[]).append((end,{'assertion_id':e['id'],'type':e['predicate'],'subject':sub,'object':obj}))
    index={e['id']:e for e in data['entities']}
    paths=[]
    def visit(nodes,edges):
        if index[nodes[-1]]['label']=='Process':
            paths.append({'nodes':nodes,'edges':edges})
        for end,edge in graph.get(nodes[-1],[]):
            if end not in nodes: visit(nodes+[end],edges+[edge])
    visit([sc['origin_id']],[])
    return {'paths':paths,'max_paths':100,'max_depth':5,'incomplete':False,'paths_limit_reached':False,'depth_limit_reached':False}
