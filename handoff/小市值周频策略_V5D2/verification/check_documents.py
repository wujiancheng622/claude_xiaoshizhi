"""Offline cross-artifact checks; no production/order/market acceptance.
Dependencies: jsonschema, beautifulsoup4, markdown-it-py. No network requests.
"""
from pathlib import Path
from urllib.parse import unquote
from fractions import Fraction
import re,json,copy,sys
from bs4 import BeautifulSoup
from markdown_it import MarkdownIt
from jsonschema import Draft202012Validator,FormatChecker
from referencing import Registry,Resource
from revision_oracle import unwire,Units,ContractError,normalize_study,select_tax,bind_dividend
P=Path(__file__).resolve().parent.parent
checks=[]
def ck(name,condition,detail=''):
 if not condition:raise AssertionError(name+' '+str(detail))
 checks.append({'name':name,'status':'PASS','detail':detail})
def readj(p):return json.loads(p.read_text())
def rejected(fn):
 try:fn()
 except (ContractError,ValueError,KeyError):return True
 return False

def main():
 s=(P/'02_测试设计与验收用例_V5D2.md').read_text()
 rows=[]
 for line in s.splitlines():
  if re.match(r'^\| G\d\d-\d\d \|',line):
   v=[x.strip() for x in line.strip('|').split('|')];rows.append((v[0],v[1:]))
 ids=[x[0] for x in rows]
 ck('case_inventory',len(ids)==338 and len(set(ids))==338,{'original':312,'added':26,'total':338})
 ck('nineteen_groups',set(x.split('-')[0] for x in ids)=={f'G{i:02d}' for i in range(1,20)})
 mf=readj(P/'verification/planned_test_cases.json')
 ck('manifest_matches_text',mf['count']==338 and [(x['id'],x['case_columns']) for x in mf['cases']]==rows)
 ck('production_cases_NOT_RUN',all(x['status']=='NOT_RUN' for x in mf['cases']),338)
 maintext=(P/'01_详细设计_V5D2.md').read_text()
 contract=(P/'03_接口与输入契约_V5D2.md').read_text()
 ck('22_design_sections',set(re.findall(r'^## (D\d\d)\.',maintext,re.M))=={f'D{i:02d}' for i in range(1,23)})
 ck('11_interface_sections',set(re.findall(r'^## (H\d\d)\.',contract,re.M))=={f'H{i:02d}' for i in range(1,12)})
 ck('risk_scope_rule_present','global_buy_blocks' in maintext and 'security_buy_blocks' in maintext and '不得再检查`any(active_risk_episode)`' in maintext)
 ck('exact_ratio_and_snapshot_defined','300004' in maintext and '分子分母均为十进制整数字符串' in maintext and 'EXACT_STATE_REPLAY_REQUIRED' in maintext)
 ck('tax_rule_basis_shared',all('EQUITY_RECORD_DATE' in t for t in [maintext,contract]))
 ck('flat_reduce_not_increase','不反向补买' in s and 'FLAT_SCORE' in maintext)
 ck('7_html_readers',len(list(P.glob('*.html')))==7)
 md=MarkdownIt('commonmark',{'html':False}).enable('table')
 norm=lambda x:re.sub(r'\s+','',x)
 for f in sorted(P.glob('*.md')):
  html=BeautifulSoup(f.with_suffix('.html').read_text(),'html.parser')
  expected=BeautifulSoup(md.render(f.read_text()),'html.parser')
  ck('reader_body_'+f.name,html.article is not None and norm(html.article.get_text())==norm(expected.get_text()))
  idlist=[x['id'] for x in html.find_all(id=True)];allids=set(idlist)
  ck('reader_unique_anchors_'+f.name,len(idlist)==len(allids))
  bad=[]
  for a in html.find_all('a',href=True):
   h=a['href']
   if h.startswith('#'):
    if h[1:] not in allids:bad.append(h)
   elif not h.startswith(('http:','https:','mailto:','data:')):
    q=unquote(h.split('#')[0])
    if not (P/q).exists():bad.append(h)
  ck('reader_links_'+f.name,not bad,bad)
  if f.name.startswith('02_'):
   domids=[]
   for tr in html.article.find_all('tr'):
    first=tr.find(['td','th'])
    if first and re.fullmatch(r'G\d\d-\d\d',first.get_text(strip=True)):domids.append(first.get_text(strip=True))
   ck('all_cases_render_as_table_rows',domids==ids,{'rendered':len(domids),'expected':len(ids)})
 schema=readj(P/'inputs/bundle.schema.json');rt=readj(P/'inputs/runtime_contracts.schema.json')
 Draft202012Validator.check_schema(schema);Draft202012Validator.check_schema(rt)
 ck('both_schemas_structurally_valid',True)
 v=Draft202012Validator(schema,format_checker=FormatChecker())
 for n in ['external_inputs.template.json','engineering_inputs.example.json']:
  x=readj(P/'inputs'/n);errs=list(v.iter_errors(x))
  ck('bundle_accepts_'+n,not errs,[e.message for e in errs])
  obj=normalize_study(x)
  ck('profile_mapping_'+n,obj['source_profile_id']==x['profiles']['sources']['profile_id'] and obj['fee_profile_id']==x['profiles']['fees']['profile_id'] and obj['baseline']==x['baseline'])
  ck('template_no_faked_verified_'+n,all(p['status']!='VERIFIED_FOR_SCOPE' for p in x['profiles'].values()))
 x=readj(P/'inputs/external_inputs.template.json')
 mutations=[('stock_budget',lambda z:z['baseline'].__setitem__('stock_target','0.70')),
 ('dd_retire',lambda z:z['baseline'].__setitem__('dd_retire','0.30')),
 ('start_years',lambda z:z['study'].__setitem__('start_years',[2016,2021,2024])),
 ('scenarios',lambda z:z['study'].__setitem__('scenarios',['S0','S1','S2'])),
 ('bool_quantity',lambda z:z['baseline'].__setitem__('slots',True)),
 ('numeric_money',lambda z:z['study'].__setitem__('capitals',[100000,500000])),
 ('verified_empty_profile',lambda z:z['profiles']['rules'].__setitem__('status','VERIFIED_FOR_SCOPE')),
 ('extra_bundle_key',lambda z:z.__setitem__('ignore_errors',True)),
 ('bad_date',lambda z:z['study'].__setitem__('end_date','2026-02-30')),
 ('live_mode',lambda z:z['study'].__setitem__('execution_mode','LIVE_ORDER')),
 ('daily_alias',lambda z:z['study'].__setitem__('execution_mode','DAILY_PROXY')),
 ('minute_alias',lambda z:z['study'].__setitem__('execution_mode','MINUTE_WINDOW')),
 ('flat_study_ref',lambda z:z['study'].__setitem__('source_profile_id','other')),
 ('old_version',lambda z:z['study'].__setitem__('detail_version','V5D1'))]
 for label,change in mutations:
  z=copy.deepcopy(x);change(z);ck('schema_rejects_'+label,bool(list(v.iter_errors(z))))
 # Local ref registry explicitly resolves fragments without remote fetch.
 base=(P/'inputs/runtime_contracts.schema.json').as_uri()
 registry=Registry().with_resources([(base,Resource.from_contents(rt)),((P/'inputs/bundle.schema.json').as_uri(),Resource.from_contents(schema))])
 rtv=Draft202012Validator(dict(rt,**{'$id':base}),registry=registry,format_checker=FormatChecker())
 ex=readj(P/'inputs/exact_state.example.json');rtv.validate(ex);Units.restore(ex['payload'])
 ck('exact_snapshot_schema_and_semantics',True)
 bad=copy.deepcopy(ex);bad['payload']['units']['num']=300004
 ck('ratio_numeric_json_rejected',bool(list(rtv.iter_errors(bad))))
 bad=copy.deepcopy(ex);bad['payload']['units']={'num':'600008','den':'6'}
 ck('unreduced_ratio_domain_rejected',rejected(lambda:Units.restore(bad['payload'])))
 bad=copy.deepcopy(ex);bad['payload']['nav']={'num':'751','den':'1000'}
 ck('inconsistent_ratio_cache_rejected',rejected(lambda:Units.restore(bad['payload'])))
 rules=readj(P/'inputs/dividend_tax_rules.test_fixture.json')['rules']
 taxschema={'$schema':schema['$schema'],'$defs':schema['$defs'],'$ref':'#/$defs/DividendTaxRule'}
 tv=Draft202012Validator(taxschema,format_checker=FormatChecker())
 ck('tax_fixtures_valid_shape',all(not list(tv.iter_errors(r)) for r in rules),len(rules))
 bad=copy.deepcopy(rules[0]);del bad['selection_date_basis']
 ck('tax_missing_selection_basis_rejected',bool(list(tv.iter_errors(bad))))
 bad=copy.deepcopy(rules[0]);bad['selection_date_basis']='PAY_DATE'
 ck('tax_pay_date_rejected',bool(list(tv.iter_errors(bad))))
 action={'record_date':'2015-09-09','pay_date':'2015-09-10'}
 binding={'kind':'DIVIDEND_BINDING','payload':{'entitlement_id':'E1','tax_profile_id':'TEST_ONLY',**bind_dividend(action,rules)}}
 rtv.validate(binding)
 ck('tax_binding_wire_matches_selection',binding['payload']['rule_selection_date']==action['record_date'] and binding['payload']['selected_rule_id']==select_tax(rules,action['record_date'])['rule_id'])
 gold=readj(P/'verification/golden_vectors.json');gids=[r['id'] for r in gold['cases']]
 ck('golden_inventory',len(gids)==32 and len(set(gids))==32)
 ck('G19_all_have_explicit_inputs_expected',{i for i in gids if i.startswith('G19-')}=={i for i in ids if i.startswith('G19-')} and all('input' in r and 'expected' in r for r in gold['cases'] if r['id'].startswith('G19-')))
 results=readj(P/'verification/revision_results.json');old=readj(P/'verification/verification_results.json')
 ck('fixed_revision_chains_executed',results['passed_chains']==26 and results['failed_chains']==0 and {x['id'] for x in results['chains']}=={i for i in ids if i.startswith('G19-')})
 ck('inherited_local_checks_executed',old['passed']==278 and old['failed']==0)
 ck('legacy_error_still_reproducible',results['legacy_decimal_counterexample']['reaches_25_percent'] is False)
 report={'detail_version':'V5D2','scope':'Cross-artifact structure, reader parity, schema/templates, normalization and exact-state/tax examples. NOT production acceptance.',
   'passed_groups':len(checks),'failed_groups':0,'planned_production_cases':338,'planned_status':'NOT_RUN','golden_groups':32,'checks':checks}
 (P/'verification/document_checks.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
 print(json.dumps({k:report[k] for k in ['passed_groups','failed_groups','planned_production_cases','golden_groups']},ensure_ascii=False))
if __name__=='__main__':main()
