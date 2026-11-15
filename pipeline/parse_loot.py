import openpyxl, json, sys, re
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
        ii=hdr.index('item'); gi=hdr.index('goes to'); ti=hdr.index('time') if 'time' in hdr else None
        ni=hdr.index('npc') if 'npc' in hdr else None
        n=0
        for r in ws.iter_rows(min_row=hrow+1,values_only=True):
            if r is None or len(r)<=gi: continue
            item=r[ii]; to=r[gi]
            if not item or not to: continue
            cost=None
            # cost: numeric in cols after gi (e.g. K)
            for c in r[gi+2:gi+6] if len(r)>gi+2 else []:
                if isinstance(c,(int,float)) and not isinstance(c,bool): cost=c
            out.append(dict(tab=name,time=str(r[ti]) if ti is not None and r[ti] else '',item=str(item).strip(),npc=str(r[ni]).strip() if ni is not None and r[ni] else '',to=str(to).strip(),cost=cost))
            n+=1
        if n: used.append((name,n))
    return out,used,skipped
if __name__=='__main__':
    o,u,s=parse(sys.argv[1]); json.dump(o,open(sys.argv[2],'w'))
    print(len(o),len(u)); print(u[:8]); print(u[-5:]); print('skipped',s[:40])
