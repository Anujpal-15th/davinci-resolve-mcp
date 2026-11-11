import json, os, pathlib, subprocess, sys, unittest

ROOT=pathlib.Path(__file__).resolve().parents[1]

class GatewayTests(unittest.TestCase):
    def test_namespaced_routing(self):
        env=os.environ.copy()
        fake=[sys.executable,str(ROOT/"tests"/"fake_mcp.py")]
        env["RESOLVE_MCP_COMMAND"]=json.dumps(fake)
        env["LEGACY_MCP_COMMAND"]=json.dumps(fake)
        proc=subprocess.Popen([sys.executable,str(ROOT/"unified_server.py")],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
            text=True,encoding="utf-8",env=env)
        def request(i,method,params=None):
            proc.stdin.write(json.dumps({"jsonrpc":"2.0","id":i,"method":method,"params":params or {}})+"\n")
            proc.stdin.flush()
            while True:
                line=proc.stdout.readline()
                if not line: raise AssertionError("gateway closed")
                data=json.loads(line)
                if data.get("id")==i: return data
        try:
            init=request(1,"initialize",{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"test","version":"1"}})
            self.assertEqual(init["result"]["serverInfo"]["name"],"resolve-creative-editor")
            proc.stdin.write(json.dumps({"jsonrpc":"2.0","method":"notifications/initialized","params":{}})+"\n")
            proc.stdin.flush()
            tools=request(3,"tools/list")["result"]["tools"]
            names={x["name"] for x in tools}
            self.assertIn("resolve__hello",names)
            self.assertIn("legacy__hello",names)
            self.assertIn("media__probe_media",names)
            result=request(4,"tools/call",{"name":"resolve__hello","arguments":{}})
            self.assertEqual(result["result"]["content"][0]["text"],"called:hello")
            result=request(5,"tools/call",{"name":"legacy__hello","arguments":{}})
            self.assertEqual(result["result"]["content"][0]["text"],"called:hello")
        finally:
            proc.terminate()
            proc.wait(timeout=5)
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream: stream.close()

if __name__=="__main__": unittest.main()
