"""Formatting, real Unicode offsets and durable delivery use synthetic accounts."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime"))
from telegram_format import render, receipt_entities_match, units


def selected(text, entity):
    data = text.encode("utf-16-le")
    return data[entity["offset"] * 2:(entity["offset"] + entity["length"]) * 2].decode("utf-16-le")


class FormattingTests(unittest.TestCase):
    def test_helper_import_does_not_need_optional_render_dependencies(self):
        env = dict(os.environ, PYTHONPATH=str(ROOT / "runtime"))
        probe = "import sys; from telegram_format import delivery_plan, receipt_entities_match; assert 'markdown_it' not in sys.modules"
        result = subprocess.run([sys.executable, "-S", "-c", probe], env=env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_screenshot_style_bold_and_named_link_with_emoji(self):
        text, entities = render("👋 • **Login works.** [Docs](https://example.com/a_(b))")
        self.assertEqual(text, "👋 • Login works. Docs")
        self.assertEqual([(e["type"], selected(text, e)) for e in entities],
                         [("bold", "Login works."), ("text_link", "Docs")])
        self.assertEqual(entities[1]["url"], "https://example.com/a_(b)")

    def test_plain_and_unmatched_markup_are_not_rewritten(self):
        for source in (" \nKeep  both\nlines.\n ", "**unfinished", "file_name.py", "2 * 3 = 6", "a\n" * 5000):
            self.assertEqual(render(source), (source, []))

    def test_code_is_literal_and_never_overlapped_by_outer_style(self):
        text, entities = render("**bold `a < b && x_y` end**\n\n```python\nprint('**literal**')\n```")
        self.assertIn("**literal**", text)
        code = [e for e in entities if e["type"] in ("code", "pre")]
        self.assertEqual(len(code), 2)
        for e in entities:
            if e["type"] not in ("code", "pre"):
                for c in code:
                    self.assertTrue(e["offset"] + e["length"] <= c["offset"] or
                                    c["offset"] + c["length"] <= e["offset"])

    def test_nested_styles_and_html_as_text(self):
        text, entities = render("**bold and *italic*** <script>literal</script> ~~old~~")
        self.assertIn("<script>literal</script>", text)
        self.assertEqual({e["type"] for e in entities}, {"bold", "italic", "strikethrough"})

    def test_relative_links_keep_their_destination_visible(self):
        text, entities = render("**File:** [source](/private/example.py)")
        self.assertEqual(text, "File: source (/private/example.py)")
        self.assertEqual([e["type"] for e in entities], ["bold"])

    def test_receipts_require_styles_and_correct_utf16_offsets(self):
        expected = [{"type": "bold", "offset": 3, "length": 4}]
        self.assertTrue(receipt_entities_match([{"type": "bold", "offset": 2, "length": 4}], expected,
                                              " 🙂bold ", "🙂bold"))
        self.assertFalse(receipt_entities_match([], expected, " 🙂bold ", "🙂bold"))
        self.assertFalse(receipt_entities_match([{"type": "bold", "offset": 1, "length": 4}], expected,
                                               " 🙂bold ", "🙂bold"))

    def test_durable_bridge_plans_and_receipts(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            private = workspace / ".private"
            private.mkdir()
            profile = private / "profile.json"
            profile.write_text(json.dumps({"owner_label": "Fixture", "bot_username": "FixtureBot"}))
            profile.chmod(0o600)
            env = dict(os.environ, COS_WORKSPACE=tmp, PYTHONPATH=str(ROOT / "runtime"))
            probe = r'''
import json
from pathlib import Path
from unittest.mock import patch
from telegram_bridge import Inbox, BridgeError, text_parts
from telegram_format import units, clip_entities
box = Inbox(Path.cwd()/"inbox")
box.db.execute("INSERT INTO users VALUES (42,42,'Fixture',0)")
box.db.commit()
class API:
 def __init__(self): self.calls=[]; self.fail=False; self.drop=False
 def call(self, method, payload):
  self.calls.append((method,payload))
  if self.fail and len(self.calls)==2: raise BridgeError('limited',429,30)
  result={'chat':{'id':42},'message_id':len(self.calls)}
  if method=='sendPhoto':
   result.update(caption=payload['caption'],caption_entities=payload.get('caption_entities',[]),
    photo=[{'file_id':'f','file_unique_id':'u','width':1,'height':1}])
  else: result.update(text=payload['text'],entities=[] if self.drop else payload.get('entities',[]))
  return result
source='**'+('🙂 long bold\n'*900).rstrip('\n')+'**\n\n[Docs](https://example.com)'
with box.db: box.reply('result:long',42,source)
row=box.db.execute("SELECT * FROM outbox WHERE id='result:long'").fetchone()
plan=json.loads(row['delivery_plan'])
assert row['body']==source and len(plan)>2
for p in plan:
 text=p['payload']['text']; assert units(text)<=4000
 for e in p['payload']['entities']:
  assert e['offset']>=0 and e['offset']+e['length']<=units(text)
  text.encode('utf-16-le')[e['offset']*2:(e['offset']+e['length'])*2].decode('utf-16-le')
assert all(any(e['type']=='bold' for e in p['payload']['entities']) for p in plan[:-1])
api=API();api.fail=True;box.flush(api)
assert box.db.execute("SELECT status FROM outbox WHERE id='result:long'").fetchone()[0]=='pending'
box.db.close();box=Inbox(Path.cwd()/"inbox")
with box.db:
 box.db.execute('UPDATE outbox SET next_attempt=0')
 box.db.execute('UPDATE outbox_parts SET next_attempt=0')
with patch('telegram_bridge.delivery_plan',side_effect=AssertionError('must not reinterpret queued text')):
 box.flush(api)
assert api.calls[1]==api.calls[2]
assert len(api.calls)==len(plan)+1
assert box.db.execute("SELECT status FROM outbox WHERE id='result:long'").fetchone()[0]=='sent'
with box.db: box.reply('result:missing-style',42,'**Must be bold**')
bad=API();bad.drop=True;box.flush(bad);box.flush(bad)
assert len(bad.calls)==1
assert box.db.execute("SELECT status FROM outbox WHERE id='result:missing-style'").fetchone()[0]=='uncertain'
with box.db: box.reply('result:photo',42,'![Guide](https://example.com/guide.png)\n**Caption** [Docs](https://example.com)')
photo=API();box.flush(photo)
assert photo.calls[0][0]=='sendPhoto' and photo.calls[0][1]['caption']=='Caption Docs'
assert len(photo.calls[0][1]['caption_entities'])==2
with box.db:
 box.db.execute("INSERT INTO outbox(id,chat_id,body,created) VALUES ('legacy',42,'**already queued literally**',1)")
legacy=API();box.flush(legacy)
assert legacy.calls[0][1]['text']=='**already queued literally**'
assert box.db.execute("SELECT status FROM outbox WHERE id='legacy'").fetchone()[0]=='sent'
box.db.close()
'''
            result = subprocess.run([sys.executable, "-c", probe], cwd=tmp, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
