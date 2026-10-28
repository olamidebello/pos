"""Formula-safe spreadsheet and CSV reports; receipts use browser print/PDF."""
import base64,csv,io,zipfile
from xml.sax.saxutils import escape

def xlsx(rows):
 def column(index):
  out=''
  while index:index,rem=divmod(index-1,26);out=chr(65+rem)+out
  return out
 sheet=['<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>']
 for index,row in enumerate(rows,1):
  cells=[]
  for col,value in enumerate(row,1):
   ref=column(col)+str(index)
   if type(value) in (int,float):cells.append('<c r="'+ref+'"><v>'+str(value)+'</v></c>')
   else:cells.append('<c r="'+ref+'" t="inlineStr"><is><t xml:space="preserve">'+escape(str(value))+'</t></is></c>')
  sheet.append('<row r="'+str(index)+'">'+''.join(cells)+'</row>')
 sheet.append('</sheetData></worksheet>');buffer=io.BytesIO()
 with zipfile.ZipFile(buffer,'w',zipfile.ZIP_DEFLATED) as z:
  z.writestr('[Content_Types].xml','<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>')
  z.writestr('_rels/.rels','<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
  z.writestr('xl/workbook.xml','<?xml version="1.0"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Sales" sheetId="1" r:id="rId1"/></sheets></workbook>')
  z.writestr('xl/_rels/workbook.xml.rels','<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>')
  z.writestr('xl/worksheets/sheet1.xml',''.join(sheet))
 return buffer.getvalue()

def report_export(report,kind):
 rows=[['Sale','Status','Currency','Total minor','Paid minor','Refunded minor','Created UTC epoch']]+[[r['id'],r['state'],r['currency'],r['total_minor'],r['paid_minor'],r['refunded_minor'],r['created']] for r in report['sales']]
 if kind=='xlsx':content=xlsx(rows);mime='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
 elif kind=='csv':
  target=io.StringIO();writer=csv.writer(target)
  for row in rows:writer.writerow(["'"+v if isinstance(v,str) and v.lstrip().startswith(('=','+','-','@')) else v for v in row])
  content=target.getvalue().encode('utf-8-sig');mime='text/csv;charset=utf-8'
 else:raise ValueError('Choose CSV or XLSX; browser print provides PDF')
 return {'filename':'global-pos-sales.'+kind,'mime':mime,'base64':base64.b64encode(content).decode()}

def qr_url(control,path,values):
 import os,re
 from pathlib import Path
 from urllib.parse import urlsplit,urlencode
 origin=os.environ.get('GLOBALPOS_PUBLIC_ORIGIN','https://pos.africandroid.com').rstrip('/');p=urlsplit(origin)
 if p.scheme!='https' or not p.hostname or p.path or p.query or p.fragment or p.username or p.password:raise ValueError('Configure the public HTTPS origin for QR links')
 if control.demo:
  identifier=Path(control.db.execute('PRAGMA database_list').fetchone()[2]).stem
  if re.fullmatch('[a-f0-9]{32}',identifier):values=dict(values,demo=identifier)
 return origin+path+'#'+urlencode(values)

def qr_svg(value):
 try:
  import qrcode,qrcode.image.svg
 except ImportError:raise ValueError('Install the QR renderer using the POS Ansible playbook') from None
 image=qrcode.make(value,image_factory=qrcode.image.svg.SvgPathImage,box_size=5,border=4);out=io.BytesIO();image.save(out)
 return {'url':value,'image':'data:image/svg+xml;base64,'+base64.b64encode(out.getvalue()).decode()}
