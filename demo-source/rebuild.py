from pathlib import Path
import json,html
p=Path(__file__).parent
data=json.loads((p/'content.json').read_text(encoding='utf-8'))
figures=json.loads((p/'figures.json').read_text(encoding='utf-8'))
script='const DATA='+json.dumps(data,ensure_ascii=False).replace('</','<\\/')+';const FIGURES='+json.dumps(figures,ensure_ascii=False).replace('</','<\\/')+';'
qr=(p/'qrcode.bundle.js').read_text(encoding='utf-8') if (p/'qrcode.bundle.js').exists() else ''
out=(p/'template.html').read_text(encoding='utf-8').replace('__TITLE__',html.escape(data['title'])).replace('__CSS__',(p/'style.css').read_text(encoding='utf-8')).replace('__DATA__',script).replace('__QR__',qr.replace('</script','<\\/script')).replace('__JS__',(p/'runtime.js').read_text(encoding='utf-8'))
(p.parent/'demo.html').write_text(out,encoding='utf-8')
print('Rebuilt demo.html')
