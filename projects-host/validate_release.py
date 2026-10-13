"""Check every bundled program, syntax and artifact path before publishing."""
import json,re,subprocess,tarfile,tempfile
from pathlib import Path
BASE=Path(__file__).resolve().parent
NODE=Path(r'C:/Users/lamon/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe')
with tarfile.open(globals().get('ARCHIVE_PATH',BASE/'build/release.tar.gz'),'r:gz') as tar:
    size=sum(m.size for m in tar.getmembers())
    assert size<100000000,size
    count=0
    for member in tar.getmembers():
        assert member.isfile() and not any(p in {'developer_key','.env','.git','node_modules'} for p in Path(member.name).parts)
        if not member.name.endswith(('.html','.js')):continue
        content=tar.extractfile(member).read().decode('utf-8-sig')
        scripts=[('',content)] if member.name.endswith('.js') else re.findall(r'<script\b([^>]*)>(.*?)</script>',content,re.S|re.I)
        for attributes,script in scripts:
            if 'application/json' in attributes or 'importmap' in attributes or not script.strip():continue
            mode='module' if 'module' in attributes or member.name.endswith('.js') else 'commonjs'
            result=subprocess.run([str(NODE),'--check','--input-type='+mode],input=script,text=True,capture_output=True,encoding='utf-8')
            if result.returncode:raise RuntimeError(member.name+'\n'+result.stderr[:2000])
            count+=1
print('Verified',count,'JavaScript programs;',size,'uncompressed bytes; no private project files packaged.')
