"""Run fixed V5D2 revision chains against an isolated reference projection.
Does not claim the 338 planned production cases or 96 market cells have run.
"""
from pathlib import Path
import json,copy,sys
from decimal import Decimal,localcontext,ROUND_HALF_UP
from fractions import Fraction
from jsonschema import Draft202012Validator,FormatChecker
from revision_oracle import (Units,OrderProjection,ratio,wire,unwire,ContractError,
    risk_to_orders,flat_action,normalize_study,select_tax,dividend_tax,reproject,bind_dividend)
P=Path(__file__).resolve().parent.parent
G=json.loads((P/'verification/golden_vectors.json').read_text())
SC=json.loads((P/'inputs/bundle.schema.json').read_text())
validator=Draft202012Validator(SC,format_checker=FormatChecker())
RULES=json.loads((P/'inputs/dividend_tax_rules.test_fixture.json').read_text())['rules']

def raises(fn):
    try:fn()
    except (ContractError,ValueError):return True
    return False

def same(actual,expected,path=''):
    if isinstance(expected,dict):
        if not isinstance(actual,dict):raise AssertionError((path,actual,expected))
        for k,v in expected.items():
            if k not in actual:raise AssertionError(('missing',path,k))
            same(actual[k],v,path+'/'+k)
    elif actual!=expected:raise AssertionError((path,actual,expected))


def order_run(inp,checkpoint=False):
    args=copy.deepcopy(inp['initial'])
    expected_fee={'purpose':'TEST_ONLY','charge_unit':'ORDER_CUMULATIVE','commission_rate':'0.00025','minimum':'5','other_fees':'0','slippage':'0','cash_settlement':'IMMEDIATE'}
    if inp['fee_fixture']!=expected_fee:raise AssertionError('unsupported fee fixture')
    for k in ['cash','limit','gross','assessed','paid']: 
        if k in args:args[k]=Decimal(args[k])
    o=OrderProjection(**args)
    steps=[]
    for step,event in enumerate(inp['events'],1):
        o.apply(event)
        for expected in inp.get('trace',[]):
            if expected['step']==step:same(o.output(),{k:v for k,v in expected.items() if k!='step'})
        steps.append(o.output())
        if checkpoint and step==inp['break_after']:
            o=OrderProjection.restore(json.loads(json.dumps(o.checkpoint())))
    result=o.output()
    if checkpoint:
        base,_=order_run(inp,False)
        o.apply(inp['events'][-1])
        result=o.output();result['restore_matches_uninterrupted']=all(result[k]==v for k,v in base.items())
    return result,steps


def run(c):
    k=c['kind'];a=c['input']
    if k=='risk_pipeline':return risk_to_orders(**a)
    if k=='capital':
        u=Units(ratio(a['equity']),unwire(a['units']),unwire(a['hwm']))
        for f in a['flows']:u.flow(f)
        return {'units':wire(u.units),**u.eod()}
    if k=='threshold_grid':
        actual=[]
        for item in a['cases']:
            u=Units(ratio(item['equity']),ratio(a['unit_count']),ratio(a['hwm']))
            u.flow('1');r=u.eod();actual.append([r['warning'],r['retire'],r['mdd_limit_pass']])
        expected=[x['flags'] for x in a['cases']]
        if actual!=expected:raise AssertionError((actual,expected))
        return {'all_flags_equal':True,'actual_flags':actual}
    if k=='exact_restore':
        u=Units(ratio('75000'),ratio('100000'))
        for f in a['flows_before']:u.flow(f)
        v=Units.restore(json.loads(json.dumps(u.snapshot())))
        for f in a['flows_after']:u.flow(f);v.flow(f)
        out={'units':wire(v.units),**v.eod(),'restore_matches_uninterrupted':v.snapshot()==u.snapshot()}
        # A pre-EOD snapshot may legitimately have NAV above the last formal HWM.
        # Restoring it must not invent an earlier EOD or reject a rising market.
        intraday=Units(ratio('110000'),ratio('100000'))
        recovered=Units.restore(intraday.snapshot())
        if recovered.hwm!=Fraction(1) or recovered.nav!=Fraction(11,10):
            raise AssertionError('pre-EOD HWM was modified')
        if recovered.eod()['dd']!=wire(Fraction(0)):
            raise AssertionError('EOD high-water mark update wrong')
        bad=v.snapshot();bad['nav']=wire(Fraction(751,1000))
        if not raises(lambda:Units.restore(bad)):raise AssertionError('mismatching exact cache accepted')
        return out
    if k=='ratio_rejection':
        bad=Units(ratio('75000'),ratio('100000')).snapshot();bad['detail_version']='V5D1'
        old=raises(lambda:Units.restore(bad))
        if not old:raise AssertionError('D1 rounded state imported')
        return {'rejected_count':sum(raises(lambda v=v:unwire(v)) for v in a['bad']),'d1_snapshot_rejected':old}
    if k in ('order','order_restore'):
        out,steps=order_run(a,k=='order_restore');out['trace_actual']=steps;return out
    if k=='flat':return flat_action(**a)
    if k=='tax_transition':
        selected=[select_tax(RULES,d) for d in a['record_dates']]
        vals=[dividend_tax(r,a['acquired'],a['disposed'],a['tax_base']) for r in selected]
        # Neither pay-date nor disposal-date selects a replacement policy row.
        actions=[{'record_date':d,'pay_date':a['pay_date'],'disposal_date':a['disposed']} for d in a['record_dates']]
        bindings=[bind_dividend(action,RULES)['selected_rule_id'] for action in actions]
        for action in actions:action['pay_date']='2019-12-31';action['disposal_date']='2020-06-01'
        again=[bind_dividend(action,RULES)['selected_rule_id'] for action in actions]
        return {'selected_rules':bindings,'final_taxes':[x['final_tax'] for x in vals],
                'additional_taxes':[x['additional'] for x in vals],'payment_date_does_not_select_rule':bindings==again}
    if k=='tax_boundaries':
        vals=[]
        for d in a['dates']:
            try:vals.append(select_tax(RULES,d)['rule_id'])
            except ContractError as e:vals.append(str(e))
        dupe=copy.deepcopy(RULES);dupe.append(copy.deepcopy(RULES[-1]));dupe[-1]['rule_id']='CONFLICT'
        overlap=raises(lambda:select_tax(dupe,'2020-01-01'))
        bad=copy.deepcopy(RULES);bad[0]['selection_date_basis']='PAY_DATE'
        bad_basis=raises(lambda:select_tax(bad,'2015-09-07'))
        return {'rules':vals,'overlap_rejected':overlap,'wrong_basis_rejected':bad_basis}
    if k=='bundle_mapping':
        b=json.loads((P/'inputs/engineering_inputs.example.json').read_text());validator.validate(b)
        n=normalize_study(b)
        extra=copy.deepcopy(b);extra['study']['source_profile_id']='INJECTED'
        ali=[]
        for mode in ['DAILY_PROXY','MINUTE_WINDOW']:
            z=copy.deepcopy(b);z['study']['execution_mode']=mode;ali.append(bool(list(validator.iter_errors(z))))
        bad=copy.deepcopy(b);bad['profiles']['sources']['payload']={'source_profile_id':'DIFFERENT'}
        return {'source_id_matches':n['source_profile_id']==b['profiles']['sources']['profile_id'],
           'factor_package_id':n['factor_package_id'],'wire_extra_profile_rejected':bool(list(validator.iter_errors(extra))),
           'aliases_rejected':all(ali),'payload_reference_mismatch_rejected':raises(lambda:normalize_study(bad))}
    if k=='late_reprojection':
        original=copy.deepcopy(a['original']);old=reproject(original,revision='OLD')
        new_events=original+[a['late']]
        corrected=reproject(new_events,revision='CORRECTED')
        bad_restart=reproject(new_events,revision='CORRECTED',checkpoint=old['snapshots'][-1])
        checkpoints=[]
        for snap in corrected['snapshots']:
            r=reproject(new_events,revision='CORRECTED',checkpoint=json.loads(json.dumps(snap)))
            checkpoints.append({z:r[z] for z in ['equity','units','nav','cash','receivable','shares']}=={z:corrected[z] for z in ['equity','units','nav','cash','receivable','shares']})
        duplicate=reproject(new_events+[copy.deepcopy(a['late'])],revision='CORRECTED')
        # This fixture has no return after correcting the omitted receivable;
        # formal NAVs are both 1, so max NAV and DD follow directly.
        hwm=max(Fraction(1),unwire(corrected['nav']))
        return {'old_units':old['units'],'corrected_equity':corrected['equity'],'corrected_units':corrected['units'],
         'corrected_nav':corrected['nav'],'corrected_hwm':wire(hwm),'corrected_dd':wire(1-unwire(corrected['nav'])/hwm),
         'shares':corrected['shares'],'cash':corrected['cash'],'receivable':corrected['receivable'],'actual_flows':corrected['actual_flows'],
         'original_facts_unchanged':original==a['original'],'all_checkpoints_agree':all(checkpoints) and bad_restart['checkpoint_rejected'] and bad_restart['units']==corrected['units'],
         'duplicate_correction_idempotent':duplicate['units']==corrected['units'] and duplicate['equity']==corrected['equity']}
    raise ValueError(k)


def main():
    out=[]
    for c in G['cases']:
        if not c['id'].startswith('G19-'):continue
        actual=run(c);same(actual,c['expected']);out.append({'id':c['id'],'status':'PASS','expected':c['expected'],'actual':actual})
    # Keep the old failing arithmetic visible as a reproduced counterexample.
    with localcontext() as ctx:
        ctx.prec=50;ctx.rounding=ROUND_HALF_UP
        e=Decimal(75000);u=Decimal(100000);v=e/u;dd=1-(e+1)/(u+1/v)
        legacy={'dd':str(dd),'reaches_25_percent':dd>=Decimal('.25')}
    if legacy['reaches_25_percent']:raise AssertionError('expected original counterexample did not reproduce')
    report={'detail_version':'V5D2','scope':'26 fixed revision chains on local reference projections, NOT a production state machine. Original312 and new26 business cases remain NOT_RUN; no 96-cell or real-account run.',
       'passed_chains':len(out),'failed_chains':0,'legacy_decimal_counterexample':legacy,'chains':out}
    (P/'verification/revision_results.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps({'passed_chains':len(out),'failed_chains':0,'legacy_counterexample_reproduced':True},ensure_ascii=False))
if __name__=='__main__':main()
