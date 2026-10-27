"""Reproducible CLI demonstrations; works on host and inside the app image."""
import json
import subprocess
from pathlib import Path
from supply_risk.overview import write_browser_queries


def run(*args):
    subprocess.run(['supply-risk',*args],check=True)


def main():
    for name in ('lenta','rossiya','pochta','synthetic'):
        run('validate',f'data/{name}.v1.json')
        run('import',f'data/{name}.v1.json')
        if name != 'synthetic':
            run('overview','--dataset',name,'--version','1','--output',f'outputs/{name}_overview')
    write_browser_queries('outputs',['lenta','rossiya','pochta'],'1')
    for i in range(1,5):
        run('assess','--dataset','lenta','--version','1','--scenario',f'S{i}',
            '--mode','evidence','--as-of','2026-10-26','--output',f'outputs/S{i}_evidence')
    for scenario,config in [('S1','update_base'),('S2','access_base'),('S3','edi_base'),('S4','disclosure')]:
        run('assess','--dataset','lenta','--version','1','--scenario',scenario,
            '--mode','experiment','--params',f'experiments/{config}.json',
            '--as-of','2026-10-26','--output',f'outputs/{scenario}_experiment')
    for scenario,before,after in [('S3','edi_base','edi_bypass'),('S3','edi_base','edi_recovery'),
                                  ('S2','access_base','access_blocked'),('S2','access_base','access_limited'),
                                  ('S1','update_base','update_blocked')]:
        run('compare','--dataset','lenta','--version','1','--scenario',scenario,
            '--before',f'experiments/{before}.json','--after',f'experiments/{after}.json',
            '--as-of','2026-10-26','--output',f'outputs/compare_{after}')
    run('sensitivity','--dataset','lenta','--version','1','--scenario','S3','--grid',
        'experiments/grid.json','--as-of','2026-10-26','--output','outputs/sensitivity')
    for scenario in ('local','common'):
        run('assess','--dataset','synthetic','--version','1','--scenario',scenario,
            '--mode','experiment','--params','experiments/synthetic.json',
            '--as-of','2026-10-26','--output',f'outputs/synthetic_{scenario}')
    for name,scenario in (('rossiya','R1'),('pochta','P1')):
        run('assess','--dataset',name,'--version','1','--scenario',scenario,
            '--mode','evidence','--as-of','2026-10-26','--output',f'outputs/{name}_historical')
        run('export','--dataset',name,'--version','1','--output',f'outputs/{name}.export.json')
    run('compare','--dataset','pochta','--version','1','--scenario','P1',
        '--before','experiments/historical_base.json','--after','experiments/pochta_access_revoked.json',
        '--as-of','2026-10-26','--output','outputs/compare_pochta_access')
    run('export','--dataset','lenta','--version','1','--output','outputs/lenta.export.json')
    run('validate','outputs/lenta.export.json')
    run_id=json.loads(Path('outputs/S3_experiment.json').read_text())['run_id']
    run('export','--dataset','lenta','--version','1','--run',run_id,'--output','outputs/exported_run')
    run('list','--dataset','lenta','--version','1','--kind','scenarios')


if __name__=='__main__': main()
