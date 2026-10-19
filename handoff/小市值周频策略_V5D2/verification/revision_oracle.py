"""V5D2 scoped reference projections, NOT a production account or trading engine.
No network, broker API, credentials, or strategy execution. Fixed fixtures only.
Money settles in Decimal at stated rounding points; exact capital/NAV uses Fraction.
"""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from decimal import Decimal, ROUND_HALF_UP
from fractions import Fraction
from datetime import date
import calendar, copy, re

D=Decimal
Q=Fraction
INT=re.compile(r'^(?:0|-?[1-9][0-9]*)$')
POS=re.compile(r'^[1-9][0-9]*$')

class ContractError(ValueError):
    pass


def ratio(x):
    if isinstance(x,bool) or isinstance(x,float):raise ContractError('INEXACT_INPUT')
    if isinstance(x,Q):return x
    if isinstance(x,int):return Q(x)
    d=D(x)
    if not d.is_finite():raise ContractError('NONFINITE_INPUT')
    return Q(d)


def wire(x):
    x=ratio(x)
    return {'num':str(x.numerator),'den':str(x.denominator)}


def unwire(x):
    if not isinstance(x,dict) or set(x)!={'num','den'}:raise ContractError('RATIO_SHAPE')
    a,b=x['num'],x['den']
    if not isinstance(a,str) or not isinstance(b,str) or not INT.fullmatch(a) or not POS.fullmatch(b):
        raise ContractError('RATIO_FORMAT')
    q=Q(int(a),int(b))
    if wire(q)!=x:raise ContractError('RATIO_NOT_CANONICAL')
    return q


def cents(x):
    return D(x).quantize(D('.01'),rounding=ROUND_HALF_UP)


def commission(gross):
    gross=D(gross)
    return cents(max(D(5),gross*D('.00025'))) if gross else D(0)


@dataclass
class Units:
    equity: Q
    units: Q
    hwm: Q=Q(1)
    projection_revision: str='TEST_V5D2'
    seq: int=0
    last_nav: Q=Q(1)

    def __post_init__(self):
        self.equity=ratio(self.equity);self.units=ratio(self.units);self.hwm=ratio(self.hwm)
        if self.units>0:self.last_nav=self.equity/self.units

    @property
    def nav(self):return self.equity/self.units if self.units else self.last_nav

    def flow(self,amount):
        f=ratio(amount)
        if self.equity<=0 or self.units<=0 or self.equity+f<0:raise ContractError('CAPITAL_INVALID')
        v=self.nav
        self.units=self.units*(self.equity+f)/self.equity
        self.equity+=f;self.last_nav=v;self.seq+=1
        if self.nav!=v:raise AssertionError('CAPITAL_CHANGED_UNIT_VALUE')

    def eod(self):
        self.hwm=max(self.hwm,self.nav)
        dd=1-self.nav/self.hwm
        return {'nav':wire(self.nav),'hwm':wire(self.hwm),'dd':wire(dd),
                'warning':dd>=Q(1,5),'retire':dd>=Q(1,4),'mdd_limit_pass':dd<=Q(3,10)}

    def snapshot(self):
        # equity in these fixed cases is an exact finite number of cents.
        e=self.equity
        if 100*e != int(100*e):raise ContractError('FIXTURE_REQUIRES_CENT_EQUITY')
        return {'detail_version':'V5D2','projection_revision':self.projection_revision,'last_committed_seq':self.seq,
                'equity':str(D(int(100*e))/100),'units':wire(self.units),'nav':wire(self.nav),
                'hwm':wire(self.hwm),'dd':wire(1-self.nav/self.hwm)}

    @classmethod
    def restore(cls,s):
        if s.get('detail_version')!='V5D2':raise ContractError('EXACT_STATE_REPLAY_REQUIRED')
        u,n,h,dd=[unwire(s[k]) for k in ['units','nav','hwm','dd']]
        obj=cls(ratio(s['equity']),u,h,s['projection_revision'],s['last_committed_seq'],n)
        if u<0 or h<=0 or obj.nav!=n or dd!=1-n/h:raise ContractError('EXACT_CACHE_MISMATCH')
        return obj


@dataclass
class OrderProjection:
    requested: int=300
    side: str='BUY'
    limit: D=D(10)
    cash: D=D(10000)
    shares: int=0
    status: str='ACKED'
    filled: int=0
    gross: D=D(0)
    assessed: D=D(0)
    paid: D=D(0)
    cancel_pending: bool=False
    cancel_required: bool=False
    cancel_reason: str=''
    acked: bool=True
    submitted: bool=True
    terminal: dict|None=None
    control_revision: int=-1
    seen: dict=field(default_factory=dict)
    trades: dict=field(default_factory=dict)
    conflicts: list=field(default_factory=list)
    capital_revision: int=0
    plan_capital_revision: int=0
    capital_invalid: bool=False
    cancellation_actions: int=0

    def _derive(self):
        if self.conflicts or self.filled>self.requested:
            self.status='RECONCILE_REQUIRED';return
        if self.terminal:
            f=self.terminal['cum_qty'];g=D(self.terminal['cum_gross'])
            if f!=self.filled or g!=self.gross:
                self.status='RECONCILE_REQUIRED';return
        if self.filled==self.requested:
            self.status='FILLED';self.cancel_pending=False;self.cancel_required=False;return
        if self.terminal:
            kind=self.terminal['status']
            if kind=='FILLED':self.status='RECONCILE_REQUIRED';return
            if kind=='REJECTED' and self.filled:
                self.status='RECONCILE_REQUIRED';return
            self.status=kind;self.cancel_pending=False;self.cancel_required=False;return
        if self.cancel_pending:self.status='CANCEL_PENDING'
        elif self.filled:self.status='PARTIAL'
        elif self.acked:self.status='ACKED'
        elif self.submitted:self.status='SENT_UNKNOWN'
        else:self.status='RESERVED'

    def apply(self,e):
        e=copy.deepcopy(e);eid=e['id']
        if eid in self.seen:
            if self.seen[eid]!=e:self.conflicts.append('CONTROL_EVENT_CONFLICT');self._derive()
            return
        self.seen[eid]=e
        k=e['kind']
        if k=='FILL':
            key=e['trade_id'];signature=(e['qty'],str(e['price']),self.side)
            if key in self.trades:
                if self.trades[key]!=signature:self.conflicts.append('TRADE_ID_CONFLICT');self._derive()
                return
            q=e['qty'];p=D(e['price'])
            if isinstance(q,bool) or not isinstance(q,int) or q<=0 or p<=0:raise ContractError('FILL_INVALID')
            self.trades[key]=signature
            self.filled+=q;self.gross+=q*p
            delta=commission(self.gross)-self.assessed
            self.assessed+=delta;self.paid+=delta
            if self.side=='BUY':self.shares+=q;self.cash-=q*p+delta
            else:self.shares-=q;self.cash+=q*p-delta
            # Confirmed local fixture records bind the already submitted attempt.
            self.submitted=True;self.acked=True
        elif k=='ACK':self.acked=True
        elif k=='CANCEL_REQUEST':
            if self.status not in {'FILLED','CANCELED','REJECTED'}:
                if not self.cancel_pending:self.cancellation_actions+=1
                self.cancel_pending=True;self.cancel_required=True;self.cancel_reason=e.get('reason','TIMEOUT')
        elif k=='CANCEL_FAILED':
            if self.status not in {'FILLED','CANCELED','REJECTED'}:
                if e.get('original_still_open') is True:self.cancel_pending=False
                else:self.conflicts.append('CANCEL_OUTCOME_UNKNOWN')
        elif k=='TERMINAL':
            version=e['source_revision'];new={x:e[x] for x in ['status','cum_qty','cum_gross']}
            if version<self.control_revision:
                return  # Explicitly comparable, superseded source version only.
            if version==self.control_revision and self.terminal!=new:
                self.conflicts.append('TERMINAL_VERSION_CONFLICT')
            else:
                self.control_revision=version;self.terminal=new
        elif k=='CAPITAL_FLOW':
            self.cash+=D(e['amount']);self.capital_revision+=1
            self.capital_invalid=True
            if self.side=='BUY' and self.status not in {'FILLED','CANCELED','REJECTED'}:
                self.cancel_required=True;self.cancel_reason='CAPITAL_CHANGED'
                if not self.cancel_pending:self.cancellation_actions+=1
                self.cancel_pending=True
        elif k in {'TARGET_REFRESH','QUOTE_RISK_REFRESH'}:
            if k=='QUOTE_RISK_REFRESH':
                mark=D(e['mark']);target=ratio(e['target_ratio'])
                equity=ratio(self.cash+self.shares*mark)
                # Natural satisfaction uses actual shares, never shares minus pending sells.
                if ratio(self.shares*mark)<=target*equity:need=0
                else:
                    need=self.remaining  # This scoped oracle only tests natural-satisfaction cancellation.
            else:need=e['remaining_required']
            if self.side=='SELL' and need < self.remaining and self.status not in {'FILLED','CANCELED','REJECTED'}:
                self.cancel_required=True;self.cancel_reason='TARGET_SATISFIED' if need==0 else 'TARGET_SHRUNK'
                if not self.cancel_pending:self.cancellation_actions+=1
                self.cancel_pending=True
        else:raise ContractError('UNSUPPORTED_FIXTURE_EVENT')
        self._derive()

    @property
    def remaining(self):
        if self.status in {'FILLED','CANCELED','REJECTED'}:return 0
        return max(self.requested-self.filled,0)

    @property
    def cash_reserved(self):
        if self.side!='BUY' or not self.remaining:return D(0)
        possible=self.remaining*self.limit
        return possible+max(commission(self.gross+possible)-self.assessed,D(0))

    def replan_probe(self):
        """A bounded post-terminal proposal check, NOT an execution engine.
        The fixture leaves one legal retry in the same window; all share changes
        are this order's fills, so q-filled equals the residual target quantity.
        General production planning must instead use its actual full account.
        """
        if self.status not in {'FILLED','CANCELED','REJECTED'}:return []
        if self.capital_invalid or self.cancel_reason in {'TARGET_SATISFIED','TARGET_SHRUNK','CAPITAL_CHANGED'}:return []
        qty=max(self.requested-self.filled,0)//100*100
        if not qty:return []
        if self.side=='BUY' and qty*self.limit+commission(qty*self.limit)>self.cash:return []
        if self.side=='SELL' and qty>self.shares:return []
        return [{'side':self.side,'qty':qty,'limit':str(self.limit)}]

    def output(self):
        return {'status':self.status,'shares':self.shares,'cash':str(self.cash),
          'filled_qty':self.filled,'gross':str(self.gross),'commission':str(self.assessed),
          'remaining_qty':self.remaining,'cash_reserved':str(self.cash_reserved),
          'sell_qty_reserved':self.remaining if self.side=='SELL' else 0,
          'capacity_used':str(self.gross),'capacity_reserved':str(self.remaining*self.limit),
          'cancel_required':self.cancel_required,'cancel_reason':self.cancel_reason,
          'replacement_orders':len(self.replan_probe()),'replacement_proposals':self.replan_probe(),'capital_invalid':self.capital_invalid}

    def checkpoint(self):
        z=asdict(self)
        for k in ['limit','cash','gross','assessed','paid']:z[k]=str(z[k])
        z['trades']={k:list(v) for k,v in z['trades'].items()}
        return z

    @classmethod
    def restore(cls,z):
        z=copy.deepcopy(z)
        for k in ['limit','cash','gross','assessed','paid']:z[k]=D(z[k])
        z['trades']={k:tuple(v) for k,v in z['trades'].items()}
        o=cls(**z);old=o.status;o._derive()
        if o.status!=old:raise ContractError('ORDER_CACHE_MISMATCH')
        if o.filled!=sum(v[0] for v in o.trades.values()):raise ContractError('ORDER_FILL_MISMATCH')
        return o


def risk_to_orders(blocked_values, total_reduce=False):
    """Small pipeline: confirmed normal sell -> actual slots -> risk -> buy intent.
    Fixed snapshots, lawful 100-share buys and a single commission; no replay engine.
    """
    blocked=[D(x) for x in blocked_values]
    nstable=29-len(blocked)
    stable=[D(1000)]*nstable
    if total_reduce:
        # 28 nonblocked holdings, total 64000, individual <=5%, spread in industries.
        stable=[D(2300)]*20+[D(2250)]*8
    held=sum(blocked)+sum(stable)
    # A 1000 CNY ordinary exit has actually filled, with 5 CNY commission.
    cash_before=D(100005)-held-D(1000)
    cash=cash_before+D(1000)-commission(D(1000))
    equity=cash+held
    assert equity==D(100000) and len(blocked)+len(stable)==29
    global_blocks=set();security_blocks={f'B{i}':['SECURITY_EXIT'] for i in range(len(blocked))}
    if ratio(sum(blocked))>=Q(1,10)*ratio(equity):global_blocks.add('BLOCKED_EXIT_EXPOSURE')
    if ratio(held)>Q(65,100)*ratio(equity):global_blocks.add('TOTAL_REDUCE')
    # Each blocked security is <=2%; the 10% case does not accidentally use a
    # single 10%-weight stock, which would also trigger SINGLE_REDUCE.
    orders=[]
    if not global_blocks and 'NEW' not in security_blocks:
        q=200;p=D(10);f=commission(q*p);epost=equity-f
        if q*p+f<=cash and held+q*p<=D('.60')*epost and q*p<=D('.05')*epost:
            orders=[{'security_id':'NEW','side':'BUY','qty':q,'limit':'10'}]
    return {'equity':str(equity),'actual_names_after_sell':29,
            'global_buy_blocks':sorted(global_blocks),'security_buy_blocks':security_blocks,
            'orders':orders}


def flat_action(qty,signal_price,execution_price):
    e=D(100000);target=D(2000);sv=qty*D(signal_price)
    if sv<=D('.025')*e:return {'mode':'HOLD_SHARES','qty':0,'side':None,'prohibit_buy':True}
    delta=qty-D(target/D(execution_price))
    sell=max(int(delta)//100*100,0)
    return {'mode':'TARGET_AMOUNT','qty':sell,'side':'SELL' if sell else None,'prohibit_buy':True}


def normalize_study(bundle):
    allowed={'study_id','detail_version','end_date','capital_mode','capitals','start_years','roles','scenarios','execution_mode','fact_snapshot_id','purpose'}
    s=bundle['study'];ps=bundle['profiles']
    if set(s)!=allowed:raise ContractError('STUDY_FIELDS')
    if s['execution_mode'] not in {'REPLAY_DAILY_PROXY','REPLAY_MINUTE_WINDOW','FORWARD_MANUAL'}:raise ContractError('MODE_INVALID')
    out=copy.deepcopy(s)
    for n,k in [('sources','source_profile_id'),('universe','universe_profile_id'),('rules','rulebook_profile_id'),('fees','fee_profile_id'),('factor','factor_profile_id'),('bootstrap','bootstrap_profile_id'),('benchmark','benchmark_profile_id')]:
        out[k]=ps[n]['profile_id']
    for n,k in [('sources','source_profile_id'),('fees','fee_profile_id')]:
        if ps[n]['payload'] is not None and ps[n]['payload'][k]!=ps[n]['profile_id']:raise ContractError('PROFILE_REFERENCE_MISMATCH')
    for n,inner,outer in [('rules','rulebook_id','rulebook_id'),('factor','package_id','factor_package_id'),('bootstrap','bootstrap_id','bootstrap_id')]:
        out[outer]=ps[n]['payload'][inner] if ps[n]['payload'] is not None else None
    out['benchmark_definition']=copy.deepcopy(ps['benchmark']['payload'])
    out['baseline']=copy.deepcopy(bundle['baseline']);out['execution_technical']=copy.deepcopy(bundle['execution_technical'])
    return out


def add_months(d,n):
    y,m=divmod(d.year*12+d.month-1+n,12);m+=1
    return date(y,m,min(d.day,calendar.monthrange(y,m)[1]))


def select_tax(rules,record_date,security_class='INDIVIDUAL_A_SHARE',dividend_type='CASH'):
    for r in rules:
        if r['selection_date_basis']!='EQUITY_RECORD_DATE':raise ContractError('TAX_SELECTION_BASIS')
        if r['effective_from']>=r['effective_to']:raise ContractError('TAX_RULE_INTERVAL')
    selected=[r for r in rules if r['security_class']==security_class and r['dividend_type']==dividend_type and r['effective_from']<=record_date<r['effective_to']]
    if not selected:raise ContractError('TAX_RULE_MISSING')
    if len(selected)>1:raise ContractError('TAX_RULE_OVERLAP')
    return selected[0]


def dividend_tax(rule,acquired,disposed,base):
    a,b=date.fromisoformat(acquired),date.fromisoformat(disposed)
    if b<a:raise ContractError('TAX_DATE_REVERSED')
    for tier in rule['tiers']:
        m=tier['max_calendar_months_inclusive']
        if m is None or b<=add_months(a,m):
            final=cents(D(base)*D(tier['rate']));initial=cents(D(base)*D(rule['initial_withholding_rate']))
            return {'rule_id':rule['rule_id'],'final_tax':str(final),'initial_withholding':str(initial),'additional':str(final-initial)}
    raise ContractError('TAX_TIER_MISSING')


def reproject(events, revision='CORRECTED', checkpoint=None):
    """Single accounting projection. Confirmed facts remain fixed; no decisions run.
    Checkpoint acceptance is intentionally narrow; unsafe ones replay from origin.
    """
    unique={}
    for e in events:
        if e['id'] in unique and unique[e['id']]!=e:raise ContractError('FACT_CONFLICT')
        unique[e['id']]=e
    ordered=sorted(unique.values(),key=lambda e:(e['effective_seq'],e['id']))
    rejected=False
    if checkpoint and checkpoint['projection_revision']!=revision:
        checkpoint=None;rejected=True
    if checkpoint:
        state=copy.deepcopy(checkpoint['state']);last=checkpoint['last_effective_seq']
        u=unwire(state['units']);cash=D(state['cash']);price=D(state['price']);ar=D(state['ar']);qty=state['qty']
    else:u=Q(0);cash=D(0);price=D(0);ar=D(0);qty=0;last=-1
    snapshots=[];actual_flows=[]
    for e in ordered:
        if e['kind']=='CASHFLOW':actual_flows.append(e['amount'])
        if e['effective_seq']<=last:continue
        k=e['kind']
        if k=='INIT':qty=e['qty'];price=D(e['price']);cash=D(e['cash']);u=ratio(cash+qty*price)
        elif k=='MARK':price=D(e['price'])
        elif k=='RECEIVABLE':ar+=D(e['amount'])
        elif k=='CASHFLOW':
            ep=ratio(cash+qty*price+ar);f=ratio(e['amount']);u=u*(ep+f)/ep;cash+=D(e['amount'])
        else:raise ContractError('UNKNOWN_ACCOUNT_FIXTURE_EVENT')
        ss={'cash':str(cash),'price':str(price),'ar':str(ar),'qty':qty,'units':wire(u)}
        snapshots.append({'projection_revision':revision,'last_effective_seq':e['effective_seq'],'state':ss})
    eq=ratio(cash+qty*price+ar);nav=eq/u
    return {'equity':str(D(eq.numerator)/D(eq.denominator)),'units':wire(u),'nav':wire(nav),
            'shares':qty,'cash':str(cash),'receivable':str(ar),'actual_flows':actual_flows,
            'checkpoint_rejected':rejected,'snapshots':snapshots}


def bind_dividend(action,rules):
    r=select_tax(rules,action['record_date'],action.get('security_class','INDIVIDUAL_A_SHARE'),action.get('dividend_type','CASH'))
    return {'record_date':action['record_date'],'rule_selection_date':action['record_date'],
            'selection_date_basis':'EQUITY_RECORD_DATE','selected_rule_id':r['rule_id']}
