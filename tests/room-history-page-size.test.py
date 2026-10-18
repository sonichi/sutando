import sys
import json
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit,parse_qs
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"skills/agent-room-ops"))
import history
class HistoryPages(unittest.TestCase):
 def run_history(self,pages):
  calls=[]
  def fetch(method,url,headers):
   query=parse_qs(urlsplit(url).query);start=int(query.get("before",["0"])[0]);limit=int(query["limit"][0]);calls.append((start,limit))
   messages=[{"event_id":str(i),"ts":300-i,"body":"safe"} for i in range(start,min(start+limit,250))]
   return 200,json.dumps({"messages":messages,"cursor":str(start+limit) if start+limit<250 else None}).encode(),{}
  with patch.object(history,"joined_rooms",return_value={"ok":True,"rooms":["!room"]}),patch.object(history,"gateway",return_value=("https://chat.ag2.space",{})),patch.object(history,"load_gate",return_value={}),patch.object(history,"gate_allows",return_value=True),patch.object(history,"_redactor",return_value=lambda x:x),patch.object(history,"http_request",side_effect=fetch):
   return history.history(60,300,pages),calls
 def test_smaller_pages_reach_cutoff_without_dropping_events(self):
  result,calls=self.run_history(20);self.assertTrue(result["ok"]);self.assertEqual(calls,[(0,100),(100,100),(200,100)]);self.assertEqual(result["window_messages"],241);self.assertEqual(result["rooms"][0]["coverage"],"reached_cutoff")
 def test_budget_exhaustion_remains_incomplete(self):
  result,calls=self.run_history(2);self.assertFalse(result["ok"]);self.assertEqual(len(calls),2);self.assertEqual(result["rooms"][0]["coverage"],"page_budget_exhausted")
if __name__=="__main__":unittest.main()
