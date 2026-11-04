# Agent mode + MCP: step-by-step guide (APAC)

This guide takes you from "v1.6.1 is on GitHub" to "I can watch Cisco AI Defense inspect an agent and its MCP tools".
Menu names in the AI Defense dashboard can differ a little between releases: the *idea* of each step is what matters.

## What you will have at the end

```
Browser ──> LabBot (Agent) ──(1) LLM, every step──> LiteLLM ──> AI Defense GATEWAY ──> Claude
                │
                └──(2) MCP tools/call──> labbot-mcp (mock tools, inside the cluster)
                        ▲
                        └── each call is also sent to the AI Defense MCP INSPECTION API:
                            the request BEFORE it runs, the result BEFORE Claude reads it
```

| You need in AI Defense | Used for | Goes into |
|---|---|---|
| **API connection** (key) | (2) the MCP checks | secret `AIDEFENSE_MCP_API_KEY` |
| **Gateway connection** (URL) | (1) the agent's LLM calls (optional: it defaults to the chat's gateway URL) | secret `AIDEFENSE_AGENT_GATEWAY_URL` |
| Region host | where the MCP checks are sent | `AIDEFENSE_BASE_URL` (already APAC: `ap.api.inspect.aidefense.security.cisco.com`) |

Part 1 and 2 need nothing from AI Defense. Part 3 to 5 are the MCP checks. Part 6 is optional.

---

## Part 1. Update the cluster

On the Ubuntu node:

```bash
cd ~/cisco-ai-defense-lab && git pull && ./scripts/deploy.sh
```

Wait until it prints the LabBot URL, then check that **four** pods are `1/1 Running`:

```bash
kubectl -n ai-defense-lab get pods
```

You should see `labbot`, `labbot-mcp`, `litellm` and `litellm-db`. If `labbot-mcp` is not ready, the chat still works; see
*Troubleshooting* at the end.

Check the version (should be `1.6.1` or higher):

```bash
cat VERSION
```

## Part 2. See the agent work (no AI Defense MCP checks yet)

1. Open `http://<ubuntu-ip>:30080` and press **Ctrl+Shift+R**.
2. In the header click **Agent**. You now see four example buttons under the message box.
3. Click **Harmless** and press **Send**.
4. In the trace you should see: the MCP tool list, `Step 1: Claude wants to use calculator`, the tool call, the result,
   and an answer around `1473.989`.

There are **no 🛡 badges** yet. That is expected: without an MCP API key the MCP checks are off (`AIDEFENSE_MCP_MODE=auto`).

If this step fails, fix it first:

| What you see | Meaning |
|---|---|
| `MCP server unavailable` | The `labbot-mcp` pod is down (`kubectl -n ai-defense-lab logs deploy/labbot-mcp`) |
| `AI gateway error 400 ...` on Step 1 | Your gateway or LiteLLM refused a request that contains tool definitions. Copy the whole message and send it to me |
| `AI gateway error 401` | The Anthropic key or the LiteLLM key is wrong |
| Step 1 shows **BLOCKED** with no policy text | The gateway connection has no enabled policy |

## Part 3. Create the AI Defense API connection for MCP checks

You need an **API** connection, not a gateway one. The Gateway application you have is for LLM traffic; keep it as it is.

1. In the AI Defense dashboard go to **Applications** and click **Add application** (or similar).
   - Name: `LabBot MCP (API)`.
   - Connection type: **API**.
2. Open the application and **add a connection**. Name it `labbot-mcp-inspection`.
3. **Generate the API key** for the connection.
   - Copy it immediately: it is shown **only once**.
   - Keep it out of chat, screenshots and git. It only belongs in the Kubernetes secret (Part 4).
4. **Attach a policy** to the connection, then enable it. Choose the rules you want applied to tool traffic and set their action
   to **Block**. Good starting rules, if your tenant offers them: prompt injection, jailbreak, PII (SSN, e-mail),
   PCI (card numbers), data exfiltration, and anything named for tools or MCP.
   - With no policy, or only monitoring rules, everything comes back "safe" and nothing is ever blocked.
5. The dashboard shows a `curl` example for this connection. It uses the host
   `ap.api.inspect.aidefense.security.cisco.com`, the same host LabBot is now configured with. If yours shows a different
   host (for example `apj.`), tell LabBot: edit `AIDEFENSE_BASE_URL` in `k8s/labbot-config.yaml` (or ask me to change it).

## Part 4. Store the key and restart LabBot

On the Ubuntu node. Paste the key between the quotes:

```bash
kubectl -n ai-defense-lab patch secret lab-secrets --type merge -p '{"stringData":{"AIDEFENSE_MCP_API_KEY":"PASTE-THE-KEY-HERE"}}'
```

LabBot reads secrets when it starts, so restart it:

```bash
kubectl -n ai-defense-lab rollout restart deploy/labbot && kubectl -n ai-defense-lab rollout status deploy/labbot
```

Check that the key is stored (this prints only the key's name and size, never its value):

```bash
kubectl -n ai-defense-lab describe secret lab-secrets | grep -i MCP
```

## Part 5. Test the connection from inside the cluster

```bash
./scripts/test-mcp-inspect.sh
```

It tests, with the same network and key as LabBot: the **chat** inspection endpoint (proves the key and region work) and
**six MCP message shapes** (what the agent sends). It sends harmless messages only and never prints the key. Each line shows
`[OK  ]` or `[FAIL]`, and the last lines explain what the pattern means.

| Output | Meaning | What to do |
|---|---|---|
| Chat `[OK  ]` and all MCP lines `[OK  ]` | **It works** | Go to Part 7 |
| Chat `[FAIL]` with `HTTP 400 ... No policies are attached to the connection (connection_id: ...)`, MCP lines `-32603` | **The most common first-time problem.** The key and region are fine, but the API connection has no enabled policy. The script prints `CAUSE FOUND` and the connection id | Open the API connection with that id in the dashboard, attach an **enabled policy** (you can reuse the chat gateway connection's policy), save, wait a minute and re-run. No restart is needed |
| Chat `[OK  ]` but every MCP line `[FAIL]` with `JSON-RPC error -32603: Internal Error` | The key and region are fine, but AI Defense fails *inside* when it handles MCP messages. This points at the connection or its policy, or at the service | 1. Make sure an **enabled policy** is attached to this connection. 2. Make sure the policy has **rules that apply to MCP / tool traffic** (add a simple one and retry). 3. If it still fails, ask Cisco support, quoting the `request id` lines. Meanwhile set `AIDEFENSE_MCP_MODE: "monitor"` (tools still run) so the agent is usable |
| Only some MCP lines fail | Some message shapes work and some do not | Send me the whole output: the app can be adapted to the shape that works |
| `HTTP 401` or `403` | The key was rejected | Use the key of an **API** connection; re-create it and repeat Part 4 |
| `HTTP 404` | Wrong host or path | Check the host in Part 3 step 5 |
| `Could not reach AI Defense ... ConnectError` | DNS or firewall | From the pod: `kubectl -n ai-defense-lab exec deploy/labbot -- python -c "import socket;print(socket.gethostbyname('ap.api.inspect.aidefense.security.cisco.com'))"`. If that fails, see *DNS problems* in the README |
| `No API key` | Part 4 did not take effect | Repeat Part 4, including the restart |

> **Best setting while MCP inspection fails:** `AIDEFENSE_MCP_FAIL_OPEN: "true"` (keep `AIDEFENSE_MCP_MODE` as `auto`). Tools are blocked only when
> AI Defense returns an **unsafe verdict**; when it returns an **error** the tool still runs and the trace shows "AI Defense error". The moment
> MCP inspection starts working, enforcement is automatic. `monitor` is the other option: it never blocks, even on a real unsafe verdict.
> The trace tells the two apart: **"Stopped: AI Defense could not check this"** (an error, fail closed) vs **"BLOCKED by Cisco AI Defense"** (a verdict).
>
> While MCP inspection fails, `AIDEFENSE_MCP_MODE: "enforce"` (the default with a key) **blocks every tool call** (fail closed) and
> the trace shows "AI Defense error". That is intended for a security tool. Use `monitor` (record only) or the Direct route to keep working.

## Part 6 (optional). Give the agent its own gateway connection

By default the agent's LLM calls use the same gateway connection as the chat. A dedicated connection lets you see the
agent's traffic separately in the dashboard.

1. Open the application with the **Gateway** badge (the one in your screenshot) and click **Add connection**.
2. Fill in:
   - **Connection name:** `labbot-agent-gateway`
   - **Data plane:** `Cisco AI Defense SaaS`
   - **Endpoint:** the **Anthropic** endpoint, the same one your chat connection uses. If it is not in the list, use
     **Add or manage endpoint** and add Anthropic's API (`https://api.anthropic.com`).
3. **Attach an enabled policy** to the new connection (without it: "No policies are attached to the connection").
4. Open the connection and **copy its Gateway URL**. It looks like
   `https://ap.gateway.aidefense.security.cisco.com/<tenant-id>/connections/<connection-id>`.
   Copy it exactly as shown, without `/v1/messages`.
5. Store it and restart LiteLLM:

```bash
kubectl -n ai-defense-lab patch secret lab-secrets --type merge -p '{"stringData":{"AIDEFENSE_AGENT_GATEWAY_URL":"PASTE-THE-GATEWAY-URL-HERE"}}'
```

```bash
kubectl -n ai-defense-lab rollout restart deploy/litellm && kubectl -n ai-defense-lab rollout status deploy/litellm
```

6. Test the agent's route end to end:

```bash
MK=$(kubectl -n ai-defense-lab get secret lab-secrets -o jsonpath='{.data.LITELLM_MASTER_KEY}' | base64 -d)
kubectl -n ai-defense-lab exec deploy/labbot -- python -c "
import httpx,sys
r=httpx.post('http://litellm:4000/v1/messages',headers={'x-api-key':sys.argv[1]},timeout=120,
  json={'model':'claude-agent','max_tokens':100,'messages':[{'role':'user','content':'Say hello'}]})
print(r.status_code, r.text[:300])" "$MK"
```

`200` with a Claude answer means the agent's LLM leg works through the new connection.

## Part 7. Run the lab scenarios

Open the page, click **Agent**, and use the example buttons. After each run, look at the **trace** (in the chat) and at the
**right-hand panel**. Every 🛡 badge shows an AI Defense **event id**.

| # | Button | Route | What to look for |
|---|---|---|---|
| 1 | **Harmless** | Via AI Defense | Three 🛡 SAFE badges: tool descriptions, request check, result check |
| 2 | **Poisoned document** | Via AI Defense | The agent reads document `kb-004`. If your policy flags the hidden instruction, the **result check is BLOCKED** and the trace says "The tool result was withheld from Claude" |
| 3 | **Poisoned document** | **Direct** (choose in the Route box) | AI Defense is bypassed. The trace shows "DIRECT, AI Defense bypassed". If Claude follows the hidden instruction you will see it list customers, fetch a record and ask to e-mail it to an outside address |
| 4 | **Needs approval** | Via AI Defense | After the checks, an **Approval needed** card appears. **Deny** = nothing is sent. **Approve** = the mock e-mail appears in the **Mock outbox** panel |
| 5 | **Exfiltration attempt** | Via AI Defense | If your policy has a matching rule, the **request check is BLOCKED before the approval question** and the e-mail never exists |

More test prompts for every category (harmless, security, privacy, safety and multi-turn scenarios) are in the **Prompt library** above the
message box; the agent ones are marked 🤖.

Notes:
- Real Claude often **refuses** the hidden instruction in scenario 3. That is a valid result, not a failure of the lab.
- What gets blocked is decided by **your policy**. If nothing is ever blocked, check Part 3 step 4.
- A blocked run does not stay in the conversation. Click **Reset** between scenarios.
- Click **Clear** in the Mock outbox panel to empty it.

## Part 8. Find the events in the AI Defense dashboard

1. Copy an event id from the trace (hover the 🛡 badge; the right-hand panel shows the full id).
2. In the dashboard open **Events** (or the connection's activity view) for the API connection `labbot-mcp-inspection`.
3. Search or filter by that id, or by the time of your test.
4. For the LLM calls of the agent, look at the events of the **gateway** connection you used (Part 6, or the chat's).

You should see, for each tool call, one event for the request and one for the result. Tool names and arguments are visible in
the event, and the JSON-RPC id looks like `1adbdfec-s2-4b0a1c` (run id, step, call id), which ties the events to one run.

What you will **not** see: a field with the agent's name. AI Defense groups events by **connection**, which is why the agent has its
own connections.

## Part 9. Experiments

| Try | How | What you learn |
|---|---|---|
| Only watch, never block | In `k8s/labbot-config.yaml` set `AIDEFENSE_MCP_MODE: "monitor"`, deploy | Verdicts show as "UNSAFE, not blocked (monitor)": useful to tune a policy |
| Poisoned tool description | Set `MCP_POISONED_TOOL: "true"`, deploy | A `currency_rates` tool appears whose *description* hides an instruction; see whether AI Defense flags the tool list |
| Outage behaviour | Temporarily set a wrong `AIDEFENSE_BASE_URL` | With `AIDEFENSE_MCP_FAIL_OPEN: "false"` every tool call is blocked (fail closed) |
| Compare | Run the same request via AI Defense and Direct | Exactly what AI Defense adds |

After editing `k8s/labbot-config.yaml` on the node, run `./scripts/deploy.sh`. Better: ask for the change so it is versioned in git.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Trace shows no 🛡 badges | No MCP key (Part 4), or you are on the Direct route |
| Badge says **AI Defense error** | Run `./scripts/test-mcp-inspect.sh` (Part 5) |
| Every tool call is blocked with an error | AI Defense unreachable and fail-closed is on. Fix the connection or use monitor mode |
| `MCP server unavailable` | `kubectl -n ai-defense-lab get pods` and `kubectl -n ai-defense-lab logs deploy/labbot-mcp` |
| `This run has expired` | An approval waited more than 10 minutes. Send the request again |
| `AI gateway error 4xx` on Step 1 | The gateway or LiteLLM did not accept the request with tools. Send me the message |
| Agent stops with "step limit" | `AGENT_MAX_STEPS` (default 8) was reached |
| The page looks old | Ctrl+Shift+R once |

## Clean up / rotate

- Rotate the MCP key: create a new key on the connection, repeat Part 4, then delete the old key in the dashboard.
- Turn the agent off: `AGENT_ENABLED: "false"` in `k8s/labbot-config.yaml`, then `./scripts/deploy.sh`.
- The MCP server and its data are **mock**: nothing is stored except an in-memory outbox that is cleared when the pod restarts.
