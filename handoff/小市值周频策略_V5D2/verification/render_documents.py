"""Offline document reader generation only; no strategy or execution code."""
from pathlib import Path
from html import escape
import re
from bs4 import BeautifulSoup
from markdown_it import MarkdownIt
P=Path(__file__).resolve().parent.parent
CSS='''
:root{--bg:#f1f4f6;--paper:#fff;--ink:#182b38;--muted:#526777;--accent:#136778;--line:#dce4e8}
*{box-sizing:border-box}html{scroll-behavior:smooth;scroll-padding-top:24px}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.85 -apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans CJK SC","Microsoft YaHei",sans-serif}a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}.layout{max-width:1510px;margin:auto;display:grid;grid-template-columns:280px minmax(0,1fr);gap:28px;padding:28px}aside{position:sticky;top:20px;align-self:start;max-height:94vh;overflow:auto;padding:22px 20px;background:#e9eff2;border:1px solid var(--line);border-radius:12px}aside h2{font-size:17px;margin:0 0 12px}aside a{display:block;font-size:13px;line-height:1.55;padding:6px 0;color:#3d596a}.subnav{padding-left:12px!important;font-size:12px!important;color:#617381!important}.version{font-size:12px;letter-spacing:.08em;color:var(--accent);font-weight:700}main{min-width:0;background:var(--paper);border:1px solid var(--line);border-radius:14px;padding:42px 44px}.topbar{display:flex;gap:16px;flex-wrap:wrap;font-size:13px;border-bottom:1px solid var(--line);padding-bottom:18px;margin-bottom:26px}h1{font-size:32px;line-height:1.4;letter-spacing:-.02em;margin:12px 0 22px}h2{font-size:24px;line-height:1.5;margin:45px 0 18px;padding-bottom:8px;border-bottom:2px solid #d6e6eb}h3{font-size:19px;margin:28px 0 12px}h4{font-size:17px;margin-top:22px}p{margin:12px 0 17px}strong{font-weight:700}blockquote{margin:20px 0;padding:12px 20px;background:#edf5f7;border-left:4px solid var(--accent)}blockquote p{margin:4px 0}.table-wrap{max-width:100%;overflow-x:auto;margin:20px 0 26px;border:1px solid var(--line);border-radius:8px}table{border-collapse:collapse;width:100%;min-width:620px;font-size:14px;line-height:1.75}th,td{padding:12px 14px;vertical-align:top;text-align:left;border-bottom:1px solid var(--line)}th{background:#e8f1f4;font-weight:700}tr:last-child td{border-bottom:0}tr:nth-child(even)td{background:#f8fafb}pre{padding:18px;background:#f2f5f7;border:1px solid var(--line);border-radius:7px;overflow:auto;font-size:13px;line-height:1.65}code{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;overflow-wrap:anywhere;font-size:.91em}p code,td code,li code{background:#eef3f5;border-radius:3px;padding:2px 4px}pre code{background:none;padding:0;white-space:pre}li{margin:6px 0}hr{border:0;border-top:1px solid var(--line);margin:30px 0}.footer{margin-top:42px;border-top:1px solid var(--line);padding-top:18px;font-size:12px;color:var(--muted)}.toc-mobile{display:none}
@media(max-width:900px){.layout{display:block;padding:12px}aside{display:none}main{padding:24px 20px;border-radius:10px}h1{font-size:25px}h2{font-size:21px;margin-top:34px}h3{font-size:18px}.toc-mobile{display:block;margin-bottom:22px;background:#edf3f5;padding:12px 16px;border-radius:8px}.toc-mobile a{display:block;font-size:13px;margin:7px 0}.table-wrap{margin:18px -3px}th,td{padding:10px 12px}body{font-size:15px}.topbar{gap:10px;font-size:12px}pre{font-size:12px}}
@media print{body{background:#fff}.layout{display:block;padding:0}aside,.topbar,.toc-mobile{display:none}main{border:0;padding:0}h2{break-after:avoid}tr{break-inside:avoid}.table-wrap{overflow:visible}table{min-width:0;font-size:10px}a{color:inherit}}
'''

def main():
 md=MarkdownIt('commonmark',{'html':False}).enable('table')
 for f in sorted(P.glob('*.md')):
  if f.name.startswith('基线_'):
   # The investment baseline Markdown/body are unchanged. Only package navigation
   # must link to this edition rather than nonexistent D1 filenames.
   hf=f.with_suffix('.html');base=hf.read_text()
   base=base.replace('_V5D1.html', '_V5D2.html')
   hf.write_text(base)
   continue
  soup=BeautifulSoup(md.render(f.read_text()),'html.parser')
  headings=[]
  for i,h in enumerate(soup.find_all(re.compile('^h[1-4]$')),1):
   h['id']=f'section-{i}'
   if h.name in ['h2','h3']:headings.append((h['id'],h.get_text(),h.name))
  for a in soup.find_all('a',href=True):
   dest=a['href'].split('#')[0]
   if not a['href'].startswith(('http:','https:','mailto:')) and dest.endswith('.md') and (P/dest).exists():
    a['href']=a['href'].replace('.md','.html',1)
  for tab in list(soup.find_all('table')):
   wrapper=soup.new_tag('div',attrs={'class':'table-wrap','tabindex':'0','aria-label':'可横向滚动的数据表'});tab.wrap(wrapper)
  title=soup.h1.get_text() if soup.h1 else f.stem
  toc=''.join(f'<a class="{"subnav" if level=="h3" else ""}" href="#{sid}">{escape(text)}</a>' for sid,text,level in headings)
  mobile=''.join(f'<a href="#{sid}">{escape(text)}</a>' for sid,text,level in headings if level=='h2')
  output=f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{escape(title)}</title><style>{CSS}</style></head><body><div class="layout"><aside><div class="version">V5D2 · 设计交付</div><h2>章节目录</h2>{toc}</aside><main><div class="topbar"><a href="README.html">交付入口</a><a href="01_详细设计_V5D2.html">详细设计</a><a href="02_测试设计与验收用例_V5D2.html">测试用例</a><a href="03_接口与输入契约_V5D2.html">接口契约</a><a href="05_逐项复核与修订裁决_V5D2.html">修订裁决</a></div><div class="version">概要 V5 不变 · 详细设计 V5D2</div><details class="toc-mobile"><summary>展开章节目录</summary>{mobile}</details><article>{str(soup)}</article><div class="footer">此阅读版与同名 Markdown 同步。局部校核不等于生产实现、市场回放或真实账户验收。</div></main></div></body></html>'''
  f.with_suffix('.html').write_text(output)
 print('Rendered 6 V5D2 readers; baseline text retained, navigation updated.')
if __name__=='__main__':main()
