"""Independent compact V5D2 arithmetic/state oracles for the design handoff.
Not production strategy code. No account, market or network access.
This companion is separate from the remote full example script; both are reported
with their actual assertion counts and must not be called full system acceptance.
"""
from __future__ import annotations
import calendar
import json
import random
from datetime import date
from decimal import Decimal as D, ROUND_HALF_UP, ROUND_FLOOR, ROUND_CEILING, localcontext
from fractions import Fraction as F
from pathlib import Path

rows=[]
def expect(cid, actual, expected):
    if actual != expected:
        raise AssertionError(f'{cid}: {actual!r} != {expected!r}')
    rows.append({'id':cid,'actual':str(actual),'expected':str(expected),'status':'PASS'})
def rank(x):
    n=len(x)
    return [F(0) for _ in x] if n<2 else [F(2*sum(y<v for y in x)+sum(y==v for y in x)-n,n-1) for v in x]
def fee(x):
    return (max(D(5),D('.00025')*x) if x>0 else D(0)).quantize(D('.01'),rounding=ROUND_HALF_UP)
def grid(q,minimum=100,step=100):
    return 0 if q<minimum else minimum+((q-minimum)//step)*step
def months(d,n):
    k=d.year*12+d.month-1+n;y,m=divmod(k,12);m+=1
    return date(y,m,min(d.day,calendar.monthrange(y,m)[1]))
def tax(a,b):
    return D('.2') if b<=months(a,1) else D('.1') if b<=months(a,12) else D(0)
def latch(active,v,e,trigger,target):return v>(target if active else trigger)*e

def main():
    expect('R01',rank([10,20,20,40]),[F(-1),F(0),F(0),F(1)])
    expect('R02',rank([7]*4),[F(0)]*4)
    expect('R03',rank([7]),[F(0)])
    expect('R04',[a+b for a,b in zip(rank([1,2,3]),rank([3,2,1]))],[F(0)]*3)
    expect('C01',5*320>=4*400,True)
    expect('C02',5*319>=4*400,False)
    u=set(range(400));ps=[u-set(range(k*40,(k+1)*40)) for k in range(3)]
    expect('C03',len(set.intersection(*ps)),280)
    expect('C04',20*95>=19*100,True)
    expect('C05',20*94>=19*100,False)
    n=0;got=[]
    for healthy,valid in [(True,False),(False,False),(False,False),(True,False),(True,True)]:
        if healthy:n=0 if valid else n+1
        got.append(n)
    expect('C06',got,[1,1,1,2,0])
    expect('F01',fee(D(0)),D(0));expect('F02',fee(D(1000)),D(5))
    expect('F03',fee(D(20000)),D(5));expect('F04',fee(D(24000)),D(6))
    expect('F05',[fee(D(x))-fee(D(y)) for x,y in [(1000,0),(3000,1000),(24000,3000)]],[D(5),D(0),D(1)])
    expect('F06',fee(D(1000))+fee(D(1000)),D(10))
    expect('Q01',grid(240),200);expect('Q02',grid(99),0)
    expect('Q03',grid(240,200,1),240);expect('Q04',grid(199,200,1),0)
    expect('Q05',grid(300-50),200)
    expect('Q06',min(1000-300,800-100),700)
    expect('L01',len(set(range(29))|{99}),30)
    expect('L02',len(set(range(29))|{30}|{30}),30)
    expect('L03',D('.6')/30*10,D('.2'))
    expect('K01',D(10000000)*D('.01')-D(40000)-D(20000),D(40000))
    expect('K02',D(10000000)*D('.01')-D(40000),D(60000))
    expect('S01',latch(False,D(65000),D(100000),D('.65'),D('.6')),False)
    expect('S02',latch(True,D(64000),D(100000),D('.65'),D('.6')),True)
    expect('S03',latch(True,D(60000),D(100000),D('.65'),D('.6')),False)
    candidates=[0]+list(range(100,6601,100))
    qnext=next(q for q in candidates if D(66000)-10*q<=D('.6')*(D(100000)-(D(5)+D('.001')*10*q if q else D(0))))
    expect('S04',qnext,700)
    expect('S05',D(60000)>D('.6')*D(99989),True)
    expect('S06',D(2000)>D('.02')*D(99900),True)
    expect('S07',D(30)+D(45)*D('.88'),D('69.60'))
    expect('S08',D(1)-D('69.60')/100,D('.304'))
    expect('S09',D(50000)+D(50000)-D(25),D(99975))
    expect('B01',D(100000)-D(30000),D(100000)-D(0)-D(30000))
    expect('B02',min(D(70000),D(80000)-D(10000)-D(2000)),D(68000))
    expect('B03',D(100000),D(100000)) # A cash reservation is not an expense.
    expect('T01',tax(date(2025,1,1),date(2025,2,1)),D('.2'))
    expect('T02',tax(date(2025,1,1),date(2025,2,2)),D('.1'))
    expect('T03',tax(date(2024,1,1),date(2025,1,1)),D('.1'))
    expect('T04',tax(date(2024,1,1),date(2025,1,2)),D(0))
    expect('T05',months(date(2025,1,31),1),date(2025,2,28))
    expect('T06',D(100)*D('.1')+D(50)*D('.2'),D(20))
    expect('A01',D(1000)+D(100)*D(9)+D(100),D(2000))
    expect('A02',D(1100)+D(100)*D(9),D(2000))
    expect('A03',F(200)*F(13,10),F(260))
    e,u,h=F(75000),F(50000),F(2);v=e/u
    expect('N01',(e+F(75000))/(u+F(75000)/v),v)
    expect('N02',1-v/h,D('.25'))
    expect('N03',(e-F(15000))/(u-F(15000)/v),v)
    with localcontext() as c:
        c.prec=40
        growth=(D('1.5')**(D(1)/10)-1).quantize(D('.00000001'))
    expect('E01',growth,D('.04137974'))
    expect('E02',False and growth>0,False)
    expect('E03',all([True,True,True,False]),False)
    expect('E04',min(map(D,['.12','.11','.07','.06','.08','.07','.05','.04'])),D('.04'))
    expect('M01',[k for k in range(5) if k<3],[0,1,2])
    expect('M02',10*60<=9*60+30,False)
    expect('M03',sorted([(60,2,'FILL'),(60,7,'CANCEL')])[0][2],'FILL')
    expect('M04',sorted([(900,2,'CLOSE_FILL'),(900,5,'RECORD')])[0][2],'CLOSE_FILL')
    tk=D('.01');px=D('10.01')
    up=lambda x:(x/tk).to_integral_value(rounding=ROUND_CEILING)*tk
    dn=lambda x:(x/tk).to_integral_value(rounding=ROUND_FLOOR)*tk
    expect('P01',up(px*D('1.003')),D('10.05'))
    expect('P02',dn(px*D('.997')),D('9.97'))
    expect('P03',up(px*D('1.003'))>dn(px*D('1.003')),True)
    # One order with partial fills, cancel pending and duplicate callback.
    requested=300;filled=0;gross=D(0);seen=set();state='ACKED';paid=D(0)
    def fill(tid,q,p):
        nonlocal filled,gross,paid
        if tid in seen:return D(0)
        assert 0<q<=requested-filled and 0<p<=10
        seen.add(tid);old=paid;filled+=q;gross+=q*p;paid=fee(gross)
        return paid-old
    def reserve():
        return D(0) if state=='CANCELED' else (requested-filled)*D(10)+max(fee(gross+(requested-filled)*D(10))-paid,D(0))
    expect('O01',reserve(),D(3005));expect('O02',fill('t1',100,D(10)),D(5))
    expect('O03',reserve(),D(2000));state='CANCEL_PENDING';expect('O04',reserve(),D(2000))
    expect('O05',fill('t1',100,D(10)),D(0));expect('O06',filled,100)
    fill('t2',50,D(10));expect('O07',filled,150);expect('O08',reserve(),D(1500))
    state='CANCELED';expect('O09',reserve(),D(0))
    cash=D(100000);hold=[]
    for _ in range(30):
        equity=cash+sum(hold)*D(10);qchoice=0
        for q in (0,100,200):
            if q*10+fee(D(q*10))<=cash and (sum(hold)+q)*10<=D('.6')*(equity-fee(D(q*10))):qchoice=q
        cash-=qchoice*10+fee(D(qchoice*10));hold.append(qchoice)
    expect('G01',sum(hold),5900);expect('G02',cash,D(40850))
    expect('G03',cash+sum(hold)*10,D(99850));expect('G04',hold[-1],100)
    expect('G05',cash+D(58800)+D(200),D(99850));expect('G06',cash+D(200),D(41050))
    expect('G07',grid(int((D(99850)*D('.02')-D(1000))/10)),0)
    rng=random.Random(20261016)
    for j in range(200):
        xs=[rng.randrange(-5,6) for _ in range(30)]
        expect(f'PR{j:03d}',rank(xs),rank([7*x+99 for x in xs]))
    result={'scope':'Companion arithmetic/local-state oracles. NOT production or full test-plan acceptance.',
            'passed':len(rows),'failed':0,'cases':rows}
    out=Path(__file__).with_name('verification_results.json');out.write_text(json.dumps(result,ensure_ascii=False,indent=2))
    print(json.dumps({'passed':len(rows),'failed':0,'output':str(out)},ensure_ascii=False))
if __name__=='__main__':main()
