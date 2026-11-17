import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
import shutil

def copy_source(destination):
    destination.mkdir()
    manifest = json.loads((REPO / 'MANIFEST.json').read_text())
    for name in set(manifest['files']) | {'MANIFEST.json'}:
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO / name, target)
    return {'manifest_sha256': hashlib.sha256((destination / 'MANIFEST.json').read_bytes()).hexdigest()}



class PackageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'
        self.receipt = copy_source(self.source)
        self.workspace = self.root / 'independent'

    def tearDown(self):
        self.temp.cleanup()

    def prepare(self, history='no', check=True, nickname=None):
        return subprocess.run([sys.executable, str(self.source / 'setup.py'), 'prepare',
                               '--directory', str(self.workspace), '--owner-label', 'Pilot Owner',
                               '--bot-username', 'IsolatedPilotBot', '--history', history] + (['--nickname', nickname] if nickname else []),
                              capture_output=True, text=True, check=check)

    def test_manifest_is_exact_and_contains_no_private_state_or_history(self):
        manifest = json.loads((self.source / 'MANIFEST.json').read_text())
        self.assertEqual(set(manifest['files']) | {'MANIFEST.json'},
                         {p.relative_to(self.source).as_posix() for p in self.source.rglob('*') if p.is_file()})
        for name, digest in manifest['files'].items():
            self.assertEqual(hashlib.sha256((self.source / name).read_bytes()).hexdigest(), digest)
        self.assertEqual(self.receipt['manifest_sha256'], hashlib.sha256((self.source / 'MANIFEST.json').read_bytes()).hexdigest())

    def test_preparation_is_private_empty_and_does_not_copy_unlisted_files(self):
        (self.source / 'runtime' / 'stray-secret.txt').write_text('private canary')
        self.prepare(nickname='Pilot')
        self.assertIn('nickname is Pilot', (self.workspace / 'COORDINATOR.md').read_text())
        self.assertEqual(json.loads((self.workspace / '.private/profile.json').read_text())['nickname'], 'Pilot')
        self.assertIn('declined conversation-history inspection', (self.workspace / 'COORDINATOR.md').read_text())
        self.assertEqual(list((self.workspace / 'tasks').glob('*/*.md')), [])
        self.assertFalse((self.workspace / 'runtime/stray-secret.txt').exists())
        self.assertFalse((self.workspace / '.private/bot-token').exists())
        self.assertFalse((self.workspace / '.private/appserver.json').exists())
        self.assertEqual((self.workspace / '.private/profile.json').stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads((self.workspace / '.private/maintenance.json').read_text()),
                         {'enabled': False, 'interval_days': 7, 'timezone': None})
        self.assertEqual((self.workspace / '.private/maintenance.json').stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.workspace / '.private').stat().st_mode & 0o777, 0o700)
        self.assertIn('.private/', (self.workspace / '.gitignore').read_text())
        again = self.prepare(check=False)
        self.assertNotEqual(again.returncode, 0)
        self.assertEqual(json.loads((self.workspace / '.private/profile.json').read_text())['owner_label'], 'Pilot Owner')

    def test_tampered_source_and_runtime_symlink_are_rejected_before_writes(self):
        target = self.source / 'runtime/alfred_local.py'
        original = target.read_bytes()
        target.write_bytes(original + b'\n# changed\n')
        self.assertNotEqual(self.prepare(check=False).returncode, 0)
        self.assertFalse(self.workspace.exists())
        target.write_bytes(original)
        real = self.root / 'runtime-elsewhere'
        (self.source / 'runtime').rename(real)
        (self.source / 'runtime').symlink_to(real, target_is_directory=True)
        self.assertNotEqual(self.prepare(check=False).returncode, 0)
        self.assertFalse(self.workspace.exists())

    def test_consent_is_explicit_and_existing_symlink_is_preserved(self):
        self.prepare('yes')
        self.assertIn('permits inspection', (self.workspace / 'COORDINATOR.md').read_text())
        self.workspace = self.root / 'alias'
        self.workspace.symlink_to(self.root / 'independent', target_is_directory=True)
        self.assertNotEqual(self.prepare(check=False).returncode, 0)
        self.assertTrue(self.workspace.is_symlink())

    def test_extracted_runtime_enrollment_reply_context_report_and_restart(self):
        self.prepare()
        probe = r'''
import asyncio, json, re, time
from pathlib import Path
from unittest.mock import AsyncMock, patch
import setup
workspace = setup.use_workspace(Path.cwd())
from telegram_bridge import Inbox
from alfred_appserver import Adapter
from operator_policy import ROOT, OWNER_LABEL, BOT
assert BOT == 'IsolatedPilotBot' and OWNER_LABEL == 'Pilot Owner'
thread = '00000000-0000-4000-8000-000000000001'
config = {'thread_id':thread, 'socket_path':str(ROOT/'fake.sock')}
inbox = Inbox(ROOT)
def update(i, uid, text, reply=None):
 msg = {'message_id':i,'date':int(time.time())+1,'from':{'id':uid},'chat':{'id':uid,'type':'private'},'text':text}
 if reply: msg['reply_to_message']={'message_id':reply,'text':'untrusted quoted text'}
 return {'update_id':i,'message':msg}
inbox.ingest(update(1,88,'Please execute something'))
assert inbox.db.execute('SELECT count(*) FROM requests').fetchone()[0] == 0
invitation=Path(inbox.invitation(OWNER_LABEL)).read_text()
code=re.search(r'start=(invite_[A-Za-z0-9_-]+)', invitation)[1]
inbox.ingest(update(2,77,'/start '+code))
assert inbox.db.execute('SELECT label FROM users WHERE user_id=77').fetchone()[0] == OWNER_LABEL
inbox.ingest(update(3,77,'Check my empty board'))
async def verify():
 adapter=Adapter(inbox, config)
 async def rpc(method, params):
  if method=='thread/turns/list': return {'data':[]}
  assert method=='turn/start'
  assert 'model' not in params and 'effort' not in params
  assert params['serviceTier']=='default'
  return {'turn':{'id':'first-turn'}}
 adapter.rpc=AsyncMock(side_effect=rpc)
 await adapter.dispatch_pending()
 params={'threadId':thread,'turnId':'first-turn','callId':'answer1','tool':'alfred_telegram_reply',
         'arguments':{'request_id':'tg:77:3','body':'Your board is empty.'}}
 assert adapter.tool_reply(params)['success']
 assert adapter.tool_reply(params)['success']
 assert not adapter.tool_reply(params | {'turnId':'wrong','callId':'foreign'})['success']
 assert inbox.db.execute("SELECT count(*) FROM outbox WHERE id='result:tg:77:3'").fetchone()[0] == 1
 with inbox.db:
  inbox.db.execute("UPDATE outbox SET status='sent',telegram_id=103 WHERE id='result:tg:77:3'")
 inbox.ingest(update(4,77,'What did I reply to?',103))
 row=inbox.db.execute("SELECT * FROM requests WHERE id='tg:77:4'").fetchone()
 text=adapter.request_text(row,'context-check')
 envelope=json.loads(text.split('\n',1)[1])
 assert envelope['sender']['authority']=='installation_owner'
 assert envelope['reply_context']['bot_message']=='Your board is empty.'
 assert 'untrusted quoted text' not in text
 inbox.ingest(update(5,77,'/report'))
 assert inbox.db.execute("SELECT count(*) FROM requests WHERE message_id=5").fetchone()[0] == 0
 report=inbox.db.execute("SELECT body FROM outbox WHERE id='result:report:tg:77:5'").fetchone()
 assert report and 'No live checks or model calls' in report[0]
 assert 'TODO (0)' in report[0] and 'DOING (0)' in report[0] and 'REVIEW (0)' in report[0]
 assert report[0].startswith('BACKLOG (0)') and 'DONE' not in report[0]
 inbox.ingest(update(6,77,'/done'))
 completed=inbox.db.execute("SELECT body FROM outbox WHERE id='result:done:tg:77:6'").fetchone()
 assert completed and completed[0].startswith('DONE — page 1 of 1')
 assert inbox.db.execute("SELECT count(*) FROM requests WHERE message_id IN (5,6)").fetchone()[0] == 0
 for styled in (False, True):
  label='**Example**' if styled else 'Example'
  body='BACKLOG (0)\n'+label+'\n'+'Saved task.\n'*600
  key='result:report:long:'+str(styled)
  inbox.reply(key,77,body)
  parts=inbox.db.execute('SELECT body FROM outbox_parts WHERE outbox_id=? ORDER BY part_index',(key,)).fetchall()
  assert len(parts)>1 and parts[0][0].startswith('BACKLOG (0)\n')
  if styled:
   plan=json.loads(inbox.db.execute('SELECT delivery_plan FROM outbox WHERE id=?',(key,)).fetchone()[0])
   assert plan[0]['payload']['entities'][0]=={'type':'bold','offset':12,'length':7}
 inbox.db.close()
 restarted=Inbox(ROOT)
 new=Adapter(restarted,config)
 assert new.thread_id==thread
 assert restarted.db.execute("SELECT status FROM requests WHERE id='tg:77:3'").fetchone()[0]=='done'
 assert restarted.db.execute("SELECT status FROM outbox WHERE id='result:tg:77:3'").fetchone()[0]=='sent'
 restarted.ingest(update(3,77,'Check my empty board'))
 assert restarted.db.execute("SELECT count(*) FROM requests WHERE id='tg:77:3'").fetchone()[0]==1
 restarted.db.close()
asyncio.run(verify())
print('isolated transport passed')
'''
        result = subprocess.run([sys.executable, '-c', probe], cwd=self.workspace, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_extracted_specialist_result_blocked_column_and_live_owner_registry(self):
        self.prepare()
        self.assertTrue((self.workspace / 'tasks/blocked').is_dir())
        probe = r'''
import asyncio, json, os, re, time
from pathlib import Path
from unittest.mock import AsyncMock
import setup
workspace = setup.use_workspace(Path.cwd())
from telegram_bridge import BridgeError, Inbox
from alfred_appserver import Adapter
from alfred_local import submit
from alfred_projects import project_route_tool
from alfred_report import report
from operator_policy import ROOT, OWNER_LABEL
thread = '00000000-0000-4000-8000-000000000002'
(ROOT/'appserver.json').write_text(json.dumps({'enabled':True,'thread_id':thread}))
os.chmod(ROOT/'appserver.json', 0o600)
inbox = Inbox(ROOT)
code = re.search(r'start=(invite_[A-Za-z0-9_-]+)', Path(inbox.invitation(OWNER_LABEL)).read_text())[1]
def update(i, text):
 return {'update_id':i,'message':{'message_id':i,'date':int(time.time())+1,'from':{'id':77},'chat':{'id':77,'type':'private'},'text':text}}
inbox.ingest(update(1,'/start '+code))
inbox.ingest(update(2,'Make the walkthrough video'))
async def verify():
 adapter = Adapter(inbox, {'thread_id':thread,'socket_path':str(ROOT/'fake.sock')})
 async def rpc(method, params):
  if method == 'thread/turns/list': return {'data':[]}
  return {'turn':{'id':'turn-1'}}
 adapter.rpc = AsyncMock(side_effect=rpc)
 await adapter.dispatch_pending()
 for parent in ('tg:77:999', None):
  try:
   submit(inbox, 'bad-'+str(parent), 'Evidence', 'project_outcome', 'codex:'+thread, parent)
   raise AssertionError('unbound specialist result accepted')
  except BridgeError:
   pass
 key = submit(inbox, 'specialist-1', 'Specialist evidence: render checked', 'project_outcome', 'codex:'+thread, 'tg:77:2')
 row = inbox.db.execute('SELECT * FROM requests WHERE id=?', (key,)).fetchone()
 text = adapter.request_text(row, 'check')
 envelope = json.loads(text.split('\n',1)[1])
 assert envelope['sender'] == {'actor':'codex:'+thread,'authority':'project_owner_evidence','role':'chief_specialist'}, envelope
 assert 'one-shot specialist' in text and 'installation_owner' not in json.dumps(envelope['sender'])
asyncio.run(verify())
schema = project_route_tool(workspace / 'projects.md')['inputSchema']['properties']['owner_id']
assert 'enum' not in schema and re.fullmatch(schema['pattern'], 'codex:' + thread)
card = workspace / 'tasks/blocked/sign-in.md'
card.write_text('# Sign in\nBlocked by: Fresh team sign-in\nResolver: Pilot Owner\nNext: Sign in\n')
assert 'BLOCKED (1)' in report(workspace) and 'Blocked by Fresh team sign-in; resolver: Pilot Owner' in report(workspace)
from alfred_local import handoff
from alfred_entry import outcome_receipt
owner = 'codex:00000000-0000-4000-8000-000000000003'
with open(workspace / 'projects.md', 'a') as index:
 index.write('| P1 | Pilot | private | `/pilot` | `' + owner + '` |\n')
inbox.ingest(update(3,'Release the shared browser'))
asyncio.run(Adapter(inbox, {'thread_id':thread,'socket_path':str(ROOT/'fake.sock')}).router.close())
with inbox.db:
 inbox.db.execute("INSERT INTO appserver_project_routes (route_id,request_id,owner_id,project,brief,input_text,input_hash,client_id,state,turn_id,created,updated) VALUES ('00000000-0000-4000-8000-0000000000aa','tg:77:3',?,'Pilot','b','i','h','c','accepted','owner-turn',1,1)", (owner,))
early = handoff(inbox, '00000000-0000-4000-8000-0000000000aa', owner, 'Browser released')
assert handoff(inbox, '00000000-0000-4000-8000-0000000000aa', owner, 'Browser released') == early
assert tuple(inbox.db.execute('SELECT state,delivered FROM appserver_project_routes').fetchone()) == ('completed', 1)
assert outcome_receipt(early, owner, ROOT)['status'] == 'found'
try:
 outcome_receipt(early, 'codex:00000000-0000-4000-8000-000000000009', ROOT)
 raise AssertionError('unregistered reader accepted')
except BridgeError:
 pass
inbox.ingest(update(4,'Only notify me when input is needed'))
async def quiet():
 adapter = Adapter(inbox, {'thread_id':thread,'socket_path':str(ROOT/'fake.sock')})
 async def rpc(method, params):
  if method == 'thread/turns/list': return {'data':[]}
  return {'turn':{'id':'turn-4'}}
 adapter.rpc = AsyncMock(side_effect=rpc)
 await adapter.dispatch_pending()
 call = {'threadId':thread,'turnId':'turn-4','callId':'quiet-1','tool':'alfred_telegram_reply',
         'arguments':{'request_id':'tg:77:4','body':'DONT_NOTIFY\nPreference saved'}}
 assert adapter.tool_reply(call)['success'] and adapter.tool_reply(call)['success']
 await adapter.router.close()
asyncio.run(quiet())
assert tuple(inbox.db.execute("SELECT status,body FROM outbox WHERE id='result:tg:77:4'").fetchone()) == ('suppressed', 'Preference saved')
from unittest.mock import Mock
api = Mock()
inbox.flush(api)
assert 'Preference saved' not in repr(api.mock_calls), api.mock_calls
assert inbox.db.execute("SELECT status FROM outbox WHERE id='result:tg:77:4'").fetchone()[0] == 'suppressed'
import alfred_entry
native = submit(inbox, 'dot-fixture', 'Reply exactly "Dot connection confirmed".')
os.chmod(ROOT/'inbox.sqlite', 0o600)
def arrive(_):
 with inbox.db:
  inbox.db.execute("INSERT INTO outbox(id,chat_id,body,status,created) VALUES (?,77,'Dot connection confirmed','pending',1)", ('result:'+native,))
waited = alfred_entry.wait_result(native, 30, ROOT, sleep=arrive, clock=lambda: 0)
assert (waited['request_id'], waited['wait_state'], waited['answer']) == (native, 'answer_saved', 'Dot connection confirmed'), waited
print('specialist and blocked passed')
'''
        result = subprocess.run([sys.executable, '-c', probe], cwd=self.workspace, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_extracted_maintenance_defaults_idle_delivery_and_attribution(self):
        self.prepare()
        probe = r'''
import asyncio, json
from pathlib import Path
from unittest.mock import AsyncMock, patch
import setup
workspace = setup.use_workspace(Path.cwd())
from telegram_bridge import Inbox
from alfred_maintenance import Maintenance, load_config, HEALTHY_NOTICE
from alfred_appserver import Adapter
from operator_policy import ROOT, OWNER_LABEL
inbox = Inbox(ROOT)
with inbox.db:
 inbox.db.execute('INSERT INTO users VALUES (77,77,?,0)', (OWNER_LABEL,))
maintenance = Maintenance(inbox, workspace)
assert load_config(ROOT) == {'enabled': False, 'interval_days': 7, 'timezone': None}
with patch('alfred_maintenance.observe') as observe:
 maintenance.tick(1805000000)
 observe.assert_not_called()
assert inbox.db.execute('SELECT count(*) FROM requests').fetchone()[0] == 0
(ROOT/'maintenance.json').write_text(json.dumps({'enabled': True, 'interval_days': 7, 'timezone': None}))
assert load_config(ROOT)['enabled'] is True
maintenance.tick(1805000000)
due = maintenance.state()['next_due']
metrics = {'free_bytes': 9999999999, 'sizes': {'workspace': 0, 'runtime': 0}}
with patch('alfred_maintenance.observe', return_value=(metrics, {})):
 assert not maintenance.tick(due)[1]
assert inbox.db.execute('SELECT body FROM outbox').fetchone()[0] == HEALTHY_NOTICE
assert inbox.db.execute('SELECT count(*) FROM requests').fetchone()[0] == 0
with patch('alfred_maintenance.observe', return_value=(metrics, {'delivery_failed': 2})):
 assert maintenance.tick(maintenance.state()['next_due'])[1]
row = inbox.db.execute('SELECT * FROM requests').fetchone()
adapter = Adapter(inbox, {'thread_id':'00000000-0000-4000-8000-000000000007', 'socket_path':'/unused'})
envelope = json.loads(adapter.request_text(row, 'fixture').split('\n', 1)[1])
assert envelope['source']['source'] == 'maintenance'
assert envelope['sender']['authority'] == 'scheduled_maintenance'
assert envelope['recipient']['authority'] == 'notification_only'
async def idle_only():
 adapter.rpc = AsyncMock(return_value={'data':[{'id':'busy','status':'inProgress'}]})
 await adapter.dispatch_pending()
 assert [c.args[0] for c in adapter.rpc.call_args_list] == ['thread/turns/list']
 adapter.rpc = AsyncMock(side_effect=[{'data':[]}, {'turn':{'id':'notice-turn'}}])
 await adapter.dispatch_pending()
 params = {'threadId':adapter.thread_id, 'turnId':'notice-turn', 'callId':'notice',
           'tool':'alfred_maintenance_notice', 'arguments':{'request_id':row['id'], 'body':'Expected downtime: 30 seconds.'}}
 result = adapter.tool_maintenance_notice(params)
 assert result['success']
 receipt = json.loads(result['contentItems'][0]['text'])
 assert receipt['sent'] is False and receipt['grants_action_authority'] is False
 assert inbox.db.execute('SELECT status FROM requests WHERE id=?',(row['id'],)).fetchone()[0] == 'processing'
 assert inbox.db.execute('SELECT chat_id FROM outbox WHERE id=?',(receipt['notice_id'],)).fetchone()[0] == 77
asyncio.run(idle_only())
inbox.db.close()
print('isolated maintenance passed')
'''
        result = subprocess.run([sys.executable, '-c', probe], cwd=self.workspace, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_thread_setup_reuses_identity_and_stops_after_uncertain_creation(self):
        self.prepare()
        probe = r'''
import asyncio, json
from pathlib import Path
from unittest.mock import AsyncMock, patch
import setup
workspace=setup.use_workspace(Path.cwd())
class Socket:
 async def send(self, text): pass
 async def __aenter__(self): return self
 async def __aexit__(self,*args): pass
async def verify():
 thread='00000000-0000-4000-8000-000000000002'
 mock=AsyncMock(side_effect=[{}, {'thread':{'id':thread}}])
 with patch('websockets.asyncio.client.unix_connect',return_value=Socket()), patch('alfred_appserver.verified_socket_path',return_value='/fake'), patch('setup.rpc',mock):
  assert await setup.create_thread(workspace)==thread
  assert await setup.create_thread(workspace)==thread
  assert mock.await_count==2
  params=mock.call_args.args[2]
  assert 'approvalPolicy' not in params and 'sandbox' not in params and 'model' not in params
  assert 'enum' not in params['dynamicTools'][1]['inputSchema']['properties']['owner_id']
  notice = params['dynamicTools'][2]
  assert notice['name'] == 'alfred_maintenance_notice'
  assert notice['inputSchema']['additionalProperties'] is False
  assert set(notice['inputSchema']['properties']) == {'request_id', 'body'}
 (workspace/'.private/appserver.json').unlink()
 mock=AsyncMock(side_effect=[{},TimeoutError()])
 with patch('websockets.asyncio.client.unix_connect',return_value=Socket()), patch('alfred_appserver.verified_socket_path',return_value='/fake'), patch('setup.rpc',mock):
  try: await setup.create_thread(workspace)
  except TimeoutError: pass
  else: raise AssertionError('must preserve uncertainty')
  try: await setup.create_thread(workspace)
  except ValueError: pass
  else: raise AssertionError('must not create duplicate thread')
  assert mock.await_count==2
 assert (workspace/'.private/thread-starting.json').exists()
 # A definite RPC rejection can retry; the uncertain fixture above cannot.
 (workspace/'.private/thread-starting.json').unlink()
 rejected=AsyncMock(side_effect=[{}, setup.RpcRejected('rejected')])
 with patch('websockets.asyncio.client.unix_connect',return_value=Socket()), patch('alfred_appserver.verified_socket_path',return_value='/fake'), patch('setup.rpc',rejected):
  try: await setup.create_thread(workspace)
  except setup.RpcRejected: pass
  else: raise AssertionError('must report rejection')
 assert not (workspace/'.private/thread-starting.json').exists()
asyncio.run(verify())
print('thread creation recovery passed')
'''
        result = subprocess.run([sys.executable, '-c', probe], cwd=self.workspace, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_wrong_bot_is_not_saved_and_failed_service_can_retry(self):
        self.prepare()
        probe = r'''
import json, plistlib, subprocess
from pathlib import Path
from unittest.mock import Mock, patch
import setup
workspace=setup.use_workspace(Path.cwd())
api=Mock()
api.call.return_value={'username':'WrongBot'}
with patch('setup.getpass.getpass', return_value='123456:'+('x'*35)), patch('setup.shutil.which',return_value='/example/codex'), patch('telegram_bridge.Telegram',return_value=api), patch('setup.subprocess.run') as run:
 try: setup.connect(workspace)
 except ValueError: pass
 else: raise AssertionError('wrong bot must be rejected')
 assert not (workspace/'.private/bot-token').exists()
 run.assert_not_called()
config={'enabled':True,'thread_id':'00000000-0000-4000-8000-000000000001','socket_path':'/fake/socket'}
setup.private_write(workspace/'.private/appserver.json', json.dumps(config))
python=workspace/'.venv/bin/python';python.parent.mkdir(parents=True);python.touch()
home=workspace/'fake-service-home';home.mkdir()
with patch('setup.sys.platform','darwin'), patch('setup.Path.home',return_value=home), patch('setup.shutil.which',return_value='/example/codex'):
 with patch('setup.subprocess.run',side_effect=subprocess.CalledProcessError(1,['launchctl'])):
  try: setup.service(workspace)
  except subprocess.CalledProcessError: pass
  else: raise AssertionError('failed bootstrap must be visible')
 assert len(list((home/'Library/LaunchAgents').glob('*.plist')))==1
 plist=next((home/'Library/LaunchAgents').glob('*.plist'))
 assert plistlib.loads(plist.read_bytes())['KeepAlive']=={'SuccessfulExit':False}
 with patch('setup.subprocess.run',side_effect=[subprocess.CompletedProcess([],1),subprocess.CompletedProcess([],0)]) as run:
  setup.service(workspace)
  assert run.call_args_list[0].args[0][1]=='print'
  assert run.call_args_list[1].args[0][1]=='bootstrap'
 with patch('setup.subprocess.run',return_value=subprocess.CompletedProcess([],0)) as run:
  setup.service(workspace)
  assert run.call_count==1 and run.call_args.args[0][1]=='print'
print('setup failure recovery passed')
'''
        result = subprocess.run([sys.executable, '-c', probe], cwd=self.workspace, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_windows_task_retries_failed_create_and_preserves_foreign_task(self):
        self.prepare()
        probe = r'''
import json, os, subprocess, types
from pathlib import Path
from unittest.mock import patch
import setup, windows_service as win
workspace=setup.use_workspace(Path.cwd())
setup.private_write(workspace/'.private/appserver.json', json.dumps({'enabled':True,'thread_id':'00000000-0000-4000-8000-000000000001','socket_path':'/fake/socket'}))
python=workspace/'.venv/bin/python';python.parent.mkdir(parents=True);python.touch()
state={'created':None,'fail':True,'runs':0,'creates':0}
def call(args, check=True):
 assert '/F' not in args and '/RP' not in args and '/RU' not in args
 if args[0].endswith('whoami.exe'):
  # Non-UTF8 console account names do not corrupt the SID.
  return subprocess.CompletedProcess(args,0,b'"pc\\r\xe9n","S-1-5-21-11-22-33-1001"\r\n')
 if args[1]=='/Query':
  body=state['created']
  if body and state.get('bad_query'): body=body.replace(b'<Command>',b'<Command>different-')
  return subprocess.CompletedProcess(args,0,body) if body else subprocess.CompletedProcess(args,1,b'')
 if args[1]=='/Create':
  state['creates']+=1
  if state['fail']: raise ValueError('fixture create failed')
  state['created']=(workspace/'.private/windows-task.xml').read_bytes()
  return subprocess.CompletedProcess(args,0,b'')
 if args[1]=='/Run':
  state['runs']+=1
  return subprocess.CompletedProcess(args,0,b'')
 if args[1]=='/Change':
  assert args[-1]=='/ENABLE'
  state['created']=win.enabled_xml(state['created'])
  return subprocess.CompletedProcess(args,0,b'')
 raise AssertionError(args)
with patch.dict(os.environ,{'WSL_DISTRO_NAME':'Ubuntu'}), patch('windows_service.platform.release',return_value='6.6-microsoft-standard-WSL2'), patch('windows_service.Path.home',return_value=workspace.parent), patch('windows_service.shutil.which',side_effect=lambda name:'/fake/'+name), patch('windows_service.pwd.getpwuid',return_value=types.SimpleNamespace(pw_name='pilot')), patch('windows_service.windows_path',side_effect=lambda path:'C:\\fixture\\'+Path(path).name), patch('windows_service.invoke',side_effect=call):
 try: win.install(workspace)
 except ValueError: pass
 else: raise AssertionError('creation failure must surface')
 assert state['runs']==0
 assert (workspace/'.private/windows-service.json').stat().st_mode & 0o777 == 0o600
 state['fail']=False
 win.install(workspace)
 assert state['creates']==2 and state['runs']==1
 win.install(workspace)
 assert state['creates']==2 and state['runs']==2
 state['created']=state['created'].replace(b'<Command>',b'<Command>changed-')
 try: win.install(workspace)
 except ValueError: pass
 else: raise AssertionError('foreign task must be preserved')
 assert state['creates']==2 and state['runs']==2
 state['created']=None;state['bad_query']=True
 try: win.install(workspace)
 except ValueError: pass
 else: raise AssertionError('post-create mismatch must stop activation')
 assert state['creates']==3 and state['runs']==2
 assert win.task_matches(state['created'],(workspace/'.private/windows-task.xml').read_bytes())
 with patch('windows_service.platform.release',return_value='generic-linux'):
  try: win.install(workspace)
  except ValueError: pass
  else: raise AssertionError('must require WSL2')
 assert state['creates']==3
print('Windows task failure recovery fixture passed')
'''
        result = subprocess.run([sys.executable, '-c', probe], cwd=self.workspace, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_windows_task_uses_current_user_without_shell_and_checks_workspace(self):
        self.prepare()
        probe = r'''
import os, subprocess, xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch
import windows_service as win
workspace=Path('/home/pilot/space & $(no-shell) "quote"')
xml=win.task_xml(workspace,'Ubuntu','pilot','S-1-5-21-11-22-33-1001',r'C:\Windows\System32\wsl.exe')
root=ET.fromstring(xml);ns={'t':win.NS}
assert root.find('t:Principals/t:Principal/t:LogonType',ns).text=='InteractiveToken'
assert root.find('t:Principals/t:Principal/t:RunLevel',ns).text=='LeastPrivilege'
assert len(root.find('t:Actions',ns))==1 and len(root.find('t:Triggers',ns))==1
assert root.find('t:Actions/t:Exec/t:Command',ns).text==r'C:\Windows\System32\wsl.exe'
args=root.find('t:Actions/t:Exec/t:Arguments',ns).text
assert args==subprocess.list2cmdline(['--distribution','Ubuntu','--user','pilot','--exec',str(workspace/'.venv/bin/python'),str(workspace/'setup.py'),'run-service','--workspace',str(workspace)])
assert win.task_matches(xml,xml)
assert root.find('t:Settings/t:Enabled',ns).text=='false'
assert not win.task_matches(win.enabled_xml(xml),xml)
assert not win.task_matches(xml.replace(b'LeastPrivilege',b'HighestAvailable'),xml)
assert not win.task_matches(xml.replace(b'</Actions>',b'<Exec><Command>other</Command></Exec></Actions>'),xml)
assert not win.task_matches(xml.replace(b'PT0S',b'PT1H'),xml)
# Windows may add defaults, which must not invalidate our explicit contract.
extra=xml.replace(b'</Settings>',b'<Hidden>false</Hidden></Settings>')
assert win.task_matches(extra,xml)
with patch.dict(os.environ,{'WSL_DISTRO_NAME':'Ubuntu'}), patch('windows_service.Path.home',return_value=Path('/home/pilot')):
 win.check_workspace(Path('/home/pilot/private'))
 for path in ('/mnt/c/private','/tmp/private'):
  try: win.check_workspace(Path(path))
  except ValueError: pass
  else: raise AssertionError('must keep private data in Linux home')
 with patch('windows_service.signal.signal') as register, patch('windows_service.os.kill') as kill:
  win.forward_hangup()
  register.call_args.args[1](None,None)
  kill.assert_called_once_with(os.getpid(),win.signal.SIGTERM)
print('Windows task scope fixture passed')
'''
        result = subprocess.run([sys.executable, '-c', probe], cwd=self.workspace, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

if __name__ == '__main__':
    unittest.main()
