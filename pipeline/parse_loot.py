import openpyxl, json, sys, re, datetime
MON={m.lower():i for i,m in enumerate(['jan','feb','mar','apr','may','jun','jul','aug','sep','oct','nov','dec'],1)}
def tab_year(name):
    m=re.search(r'\b\d{1,2}/\d{1,2}/(\d{2,4})\b',name)
    if m: y=int(m.group(1)); return y+2000 if y<100 else y
    s=name.strip().split(' ')[0]
    if re.fullmatch(r'\d{8}',s) and 2015<int(s[4:])<2100: return int(s[4:])          # 11092026 (Google's export drops the slashes)
    if re.fullmatch(r'\d{6}',s): return 2000+int(s[4:])                                 # 111526
    return None
def text_time(s,name):
    """'Nov 15 19:49:51' or 'November 15 19:49:52' (no year): the year comes from the tab name (e.g. 11/15/26), else this year."""
    m=re.match(r'^([A-Za-z]{3,9})\.? +(\d{1,2}),? +(\d{1,2}):(\d\d)(?::(\d\d))?',s.strip())
    if not m or m.group(1)[:3].lower() not in MON: return None
    y=tab_year(name); guess=y is None
    if guess: y=datetime.date.today().year          # tabs named like 'TOB 11/15' carry no year
    try: d=datetime.datetime(y,MON[m.group(1)[:3].lower()],int(m.group(2)),int(m.group(3)),int(m.group(4)),int(m.group(5) or 0))
    except ValueError: return None
    if guess and d.date()>datetime.date.today()+datetime.timedelta(days=7): d=d.replace(year=y-1)   # a December tab read in January is last year's
    return d.isoformat(sep=' ')
def cell_time(v,name):
    if v is None: return ''
    if hasattr(v,'year'): return str(v)[:19]
    s=str(v).strip()
    return text_time(s,name) or s
def parse(path):
    wb=openpyxl.load_workbook(path,read_only=True,data_only=True)
    out=[]; used=[]; skipped=[]
    for ws in wb.worksheets:
        name=ws.title
        if re.search(r'(?i)template|master|audit|roll|tier|rot list|sheet\d|tracker|attendance|^\.+$|^\d{1,2}$|requested|initial|do not delete|donotuse', name): skipped.append(name); continue
        rows=ws.iter_rows(min_row=1,max_row=4,values_only=True)
        hdr=None; hrow=0
        for i,r in enumerate(rows,1):
            cells=[str(c).strip().lower() if c is not None else '' for c in r]
            if 'item' in cells and 'goes to' in cells: hdr=cells; hrow=i; break
        if not hdr: skipped.append(name); continue
        ii=hdr.index('item'); gi=hdr.index('goes to'); ti=hdr.index('time') if 'time' in hdr else hdr.index('date') if 'date' in hdr else None   # date looted (column is 'Time' or 'Date')
        if ti is None and re.match(r'\d{4}-\d\d-\d\d',hdr[0]): ti=0   # some tabs have a date typed over the 'Time' header in A1
        ni=hdr.index('npc') if 'npc' in hdr else None
        ci=hdr.index('class') if 'class' in hdr and hdr.index('class')<10 else None   # the class the item was bid for (Faceless Fury, column H)
        ri=hdr.index('round') if 'round' in hdr else (8 if len(hdr)>8 and hdr[8]=='' else None)   # the bidding round: a 'Round' column, else column I if it has no other header
        n=0
        for r in ws.iter_rows(min_row=hrow+1,values_only=True):
            if r is None or len(r)<=gi: continue
            item=r[ii]; to=r[gi]
            if not item or not to: continue
            cost=None   # Glask doesn't use DKP: costs are never read
            rnd=str(r[ri]).strip() if ri is not None and len(r)>ri and r[ri] is not None else ''   # Faceless Fury: round 1 = main round, 2+ = FF round
            out.append(dict(tab=name,time=cell_time(r[ti],name) if ti is not None else '',item=str(item).strip(),npc=str(r[ni]).strip() if ni is not None and r[ni] else '',to=str(to).strip(),cost=cost,round=rnd,cls=str(r[ci]).strip() if ci is not None and len(r)>ci and r[ci] else ''))
            n+=1
        if n: used.append((name,n))
    return out,used,skipped
if __name__=='__main__':
    o,u,s=parse(sys.argv[1]); json.dump(o,open(sys.argv[2],'w'))
    print(len(o),len(u)); print(u[:8]); print(u[-5:]); print('skipped',s[:40])
