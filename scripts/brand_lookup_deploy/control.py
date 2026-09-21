"""User-run activation or rollback. Does not send messages or generate newsletters."""
import hashlib,json,os,re,shutil,subprocess,sys
from pathlib import Path
root=Path(__file__).resolve().parents[2]
release=json.loads((root/'scripts/brand_lookup_release.json').read_text());backup=Path(release['backup'])
mode=sys.argv[1] if len(sys.argv)==2 else ''
if mode not in {'activate','rollback'}:raise SystemExit('Usage: control.py activate|rollback')
for rel,info in release['files'].items():
 if hashlib.sha256((root/rel).read_bytes()).hexdigest()!=info['installed_sha256']:
  raise SystemExit('File changed after installation; inspect before activation/rollback: '+rel)
services=['ai-bot-receiver.service','ai-bot-workers.service','ai-bot-document-workers.service']
for service in services:
 result=subprocess.run(['systemctl','show',service,'--property=LoadState','--value'],capture_output=True,text=True,check=True)
 if result.stdout.strip()!='loaded':raise SystemExit('Service unavailable: '+service)
env=root/'.env';content=env.read_text();pattern=r'(?m)^(?:export\s+)?BRAND_LOOKUP_(?:ENABLED|BOT_PREVIEW)\s*=.*\n?'
flags=backup/'flags-before.json'
if mode=='activate':
 subprocess.run([str(root/'.venv/bin/python'),str(root/'scripts/brand_lookup_deploy/preflight.py'),str(root)],cwd=root,env={**os.environ,'PYTHONPATH':str(root)},check=True)
 if not flags.exists():flags.write_text(json.dumps(re.findall(pattern,content)));flags.chmod(0o600)
 content=re.sub(pattern,'',content).rstrip()+'\nBRAND_LOOKUP_ENABLED=1\nBRAND_LOOKUP_BOT_PREVIEW=1\n'
else:
 if not flags.exists():raise SystemExit('No activation recorded')
 content=re.sub(pattern,'',content).rstrip()+'\n'+''.join(line.rstrip()+'\n' for line in json.loads(flags.read_text()))
 for rel,info in release['files'].items():
  if info['existed']:shutil.copy2(backup/rel,root/rel)
  else:(root/rel).unlink(missing_ok=True)
stage=env.with_name('.env.brand-stage');stage.write_text(content);stage.chmod(env.stat().st_mode & 0o777);os.replace(stage,env)
subprocess.run(['systemctl','restart',*services],check=True)
subprocess.run(['systemctl','is-active',*services],check=True)
print('Brand lookup '+mode+' complete. Services restarted; no message or newsletter sent.')
