from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path


PLUGIN = (
    Path(__file__).parents[1]
    / "src"
    / "sn_proactive_agent_connectors"
    / "openclaw"
    / "plugin"
    / "index.js"
)


class OpenClawPluginTests(unittest.TestCase):
    def test_web_session_key_resumes_persisted_session_and_keeps_public_identity(self) -> None:
        script = f"""
        import {{ Readable }} from 'node:stream';
        const {{ default: plugin }} = await import({json.dumps(PLUGIN.as_uri())});
        const calls = []; let actual;
        const api = {{
          pluginConfig: {{ serviceUrl: 'http://web-resume.test' }}, config: {{}}, logger: {{warn() {{}}}},
          on(n,h) {{this[n]=h}}, registerHttpRoute(r) {{this.route=r}},
          runtime: {{agent: {{
            session: {{listSessionEntries: () => [{{sessionKey:'agent:main:main', entry:{{sessionId:'uuid-123'}}}}]}},
            resolveAgentTimeoutMs: () => 1000,
            async runEmbeddedAgent(p) {{
              actual = p.sessionId;
              const ctx={{sessionId:p.sessionId,sessionKey:p.sessionKey,runId:p.runId}};
              await api.before_agent_run({{prompt:p.prompt}},ctx);
              await api.agent_end({{success:true,messages:[{{role:'user',content:p.prompt}},{{role:'assistant',content:'Saved'}}]}},ctx);
              return {{meta:{{}}}};
            }}
          }}}}
        }};
        plugin.register(api);
        globalThis.fetch=async (url,o)=>{{calls.push({{url,body:JSON.parse(o.body)}});return {{ok:true}}}};
        const req=Readable.from([JSON.stringify({{suggestion_id:'web-s1',session_id:'agent:main:main',suggested_action:'Update report'}})]);
        req.method='POST';req.headers={{}};
        const res={{setHeader(){{}},end(){{}}}};
        await api.route.handler(req,res);
        await new Promise(r=>setTimeout(r,20));
        console.log(JSON.stringify({{actual,status:res.statusCode,calls}}));
        """
        result = subprocess.run(["node", "--input-type=module", "-e", script], text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data["actual"], "uuid-123")
        self.assertEqual(data["status"], 202)
        self.assertEqual([x["body"]["session_id"] for x in data["calls"]], ["agent:main:main"] * 2)
        self.assertEqual(data["calls"][1]["body"]["source_suggestion_id"], "web-s1")

    def test_resume_across_plugin_instances_is_correlated_and_not_duplicated(self) -> None:
        script = f"""
        import {{ Readable }} from 'node:stream';
        const {{ default: plugin }} = await import({json.dumps(PLUGIN.as_uri())});
        const calls = [];
        let finish, runs = 0;
        const gate = new Promise(resolve => {{ finish = resolve; }});
        const common = {{
          pluginConfig: {{ serviceUrl: 'http://resume.test' }},
          config: {{}}, logger: {{ warn() {{}} }},
          on(name, handler) {{ this[name] = handler; }},
          registerHttpRoute(route) {{ this.route = route; }},
        }};
        const hooks = {{ ...common }};
        plugin.register(hooks);
        const routeApi = {{ ...common, runtime: {{ agent: {{
          session: {{ listSessionEntries: () => [{{ sessionKey: 'agent:main:demo', entry: {{ sessionId: 'session-1' }} }}] }},
          resolveAgentTimeoutMs: () => 1000,
          async runEmbeddedAgent(p) {{
            runs++;
            await gate;
            const ctx = {{ sessionId: p.sessionId, sessionKey: p.sessionKey, runId: p.runId }};
            await hooks.before_agent_run({{ prompt: p.prompt }}, ctx);
            await hooks.agent_end({{ success: true, runId: p.runId, messages: [
              {{role: 'user', content: p.prompt}}, {{role: 'assistant', content: 'Updated.'}}
            ] }}, ctx);
            return {{meta: {{}}}};
          }}
        }} }} }};
        plugin.register(routeApi);
        globalThis.fetch = async (url, options) => {{ calls.push({{url, body: JSON.parse(options.body)}}); return {{ok: true}}; }};
        const body = {{suggestion_id: 'suggestion-1', session_id: 'session-1', suggested_action: 'Update report'}};
        async function submit(value, headers={{}}) {{
          const req = Readable.from([JSON.stringify(value)]); req.method='POST'; req.headers=headers;
          const res = {{setHeader() {{}}, end(value) {{this.body=JSON.parse(value)}}}};
          await routeApi.route.handler(req, res); return res;
        }}
        const first = await submit(body);
        const retry = await submit(body);
        const conflict = await submit({{...body, suggested_action:'Other'}});
        const browser = await submit(body, {{origin: 'http://untrusted.test'}});
        finish();
        await new Promise(resolve => setTimeout(resolve, 20));
        const ordinary = {{sessionId:'session-1', sessionKey:'agent:main:demo', runId:'ordinary'}};
        await hooks.before_agent_run({{prompt:'Update report'}}, ordinary);
        await hooks.agent_end({{success:true, runId:'ordinary', messages:[{{role:'assistant', content:'Ordinary reply'}}]}}, ordinary);
        console.log(JSON.stringify({{auth: routeApi.route.auth, first:first.statusCode, retry:retry.statusCode,
          conflict:conflict.statusCode, browser:browser.statusCode, runs, calls}}));
        """
        result = subprocess.run(["node", "--input-type=module", "-e", script],
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data["auth"], "gateway")
        self.assertEqual([data[k] for k in ("first", "retry", "conflict", "browser")], [202, 202, 400, 400])
        self.assertEqual(data["runs"], 1)
        completed = [x["body"] for x in data["calls"] if x["url"].endswith("turn.completed")]
        self.assertEqual(len(completed), 2)
        self.assertEqual(completed[0]["source_suggestion_id"], "suggestion-1")
        self.assertNotIn("source_suggestion_id", completed[1])

    def test_plugin_is_valid_esm_and_maps_a_complete_turn(self) -> None:
        script = f"""
        const plugin = await import({json.dumps(PLUGIN.as_uri())});
        const calls = [];
        const api = {{
          pluginConfig: {{ serviceUrl: 'http://service.test' }},
          logger: {{ warn: () => {{}} }},
          on(name, handler) {{ this[name] = handler; }},
        }};
        plugin.default.register(api);
        globalThis.fetch = async (url, options) => {{
          calls.push({{ url, body: JSON.parse(options.body) }});
          return {{ ok: true, status: 202 }};
        }};
        await api.before_agent_run({{ prompt: '整理本周项目进展' }}, {{ sessionKey: 'openclaw:main', runId: 'run-1' }});
        await api.agent_end({{ runId: 'run-1', messages: [
          {{ role: 'user', content: '整理本周项目进展' }},
          {{ role: 'assistant', content: [{{ type: 'text', text: '已整理本周项目进展。' }}] }}
        ] }}, {{ sessionKey: 'openclaw:main', runId: 'run-1' }});
        console.log(JSON.stringify(calls));
        """
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = json.loads(result.stdout)
        self.assertEqual([item["url"] for item in calls], [
            "http://service.test/v1/events/turn.started",
            "http://service.test/v1/events/turn.completed",
        ])
        self.assertEqual(calls[1]["body"]["platform"], "openclaw")
        self.assertEqual(calls[1]["body"]["session_id"], "openclaw:main")
        self.assertEqual(calls[1]["body"]["user_question"], "整理本周项目进展")
        self.assertEqual(calls[1]["body"]["final_answer"], "已整理本周项目进展。")

    def test_channel_and_agent_hooks_do_not_duplicate_turn_started(self) -> None:
        script = f"""
        const plugin = await import({json.dumps(PLUGIN.as_uri())});
        const calls = [];
        const api = {{
          pluginConfig: {{ serviceUrl: 'http://service.test' }},
          logger: {{ warn: () => {{}} }},
          on(name, handler) {{ this[name] = handler; }},
        }};
        plugin.default.register(api);
        globalThis.fetch = async (url, options) => {{
          calls.push({{ url, body: JSON.parse(options.body) }});
          return {{ ok: true, status: 202 }};
        }};
        await api.message_received({{ content: '同一轮消息', messageId: 'msg-1' }}, {{ sessionKey: 'openclaw:main', runId: 'run-1' }});
        await api.before_agent_run({{ prompt: '同一轮消息' }}, {{ sessionKey: 'openclaw:main', runId: 'run-1' }});
        console.log(JSON.stringify(calls));
        """
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = json.loads(result.stdout)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["url"], "http://service.test/v1/events/turn.started")

    def test_manifest_and_package_are_present(self) -> None:
        manifest = json.loads((PLUGIN.parent / "openclaw.plugin.json").read_text())
        self.assertEqual(manifest["id"], "sn-proactive-agent")
        self.assertTrue((PLUGIN.parent / "package.json").is_file())


if __name__ == "__main__":
    unittest.main()
