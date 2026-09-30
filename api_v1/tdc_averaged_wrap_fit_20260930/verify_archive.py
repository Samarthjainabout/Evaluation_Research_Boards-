"""Offline archival integrity and exact model reproduction. No hardware access."""
import hashlib
import json
from pathlib import Path
import statistics
import sys

HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[1]
sys.path.insert(0,str(ROOT/'api_v1/publication'))
from analyze_existing_pair_calibration import analyze


def main():
    manifest=json.loads((HERE/'artifact_manifest.json').read_text())
    for item in manifest['files']:
        data=(ROOT/item['path']).read_bytes()
        if item['hash_encoding']=='LF-normalized UTF-8':
            data=data.replace(b'\r\n',b'\n')
        assert hashlib.sha256(data).hexdigest()==item['sha256'], item['path']
    report=json.loads((HERE/'data/report.json').read_text())
    inputs=json.loads((HERE/'fit_inputs.json').read_text())['checkpoints']
    assert len(inputs)==len(report['states'])==8
    assert len({s['row'] for s in report['states']})==7
    assert sum(len(s['wb']) for s in report['states'])==90
    for s,record in zip(report['states'],inputs):
        ticks=[100*w['selected_matching']['coarse_cnt']+99-w['selected_matching']['fine_cnt'] for w in s['wb']]
        delta=statistics.mean(x['G31_minus_G30_uS'] for x in s['before']+s['after'])
        assert abs(statistics.mean(ticks)-record['T_mean'])<1e-9
        assert abs(delta-record['G31_minus_G30_uS'])<1e-9
        assert abs(record['row_weight']-1/sum(x['row']==s['row'] for x in report['states']))<1e-12
    fitted=analyze(report['states'])
    for name,expected in report['analysis']['models'].items():
        actual=fitted['models'][name]
        for a,b in zip(actual['coefficients'],expected['coefficients']):
            assert abs(a-b)<1e-8,(name,a,b)
        assert abs(actual['grouped_cv_rmse_uS']-expected['grouped_cv_rmse_uS'])<1e-8,name
    print(json.dumps({'verified_files':len(manifest['files']),'checkpoints':8,'independent_rows':7,
                      'accepted_WB_conversions':90,'primary_fit':fitted['models']['wrap100_minus']['coefficients'],
                      'held_out_RMSE_uS':fitted['models']['wrap100_minus']['grouped_cv_rmse_uS']},indent=2))


if __name__=='__main__':
    main()
