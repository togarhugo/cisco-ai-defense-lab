// Browser test for the prompt library and the multi-turn player (needs Playwright + Chromium; not part of CI).
// The page and /api/prompts come from the real app; /api/chat and /api/agent/run are stubbed.
//
//   (APP_VERSION=test AIDEFENSE_MODE=gateway LLM_BASE_URL=http://127.0.0.1:1 LLM_API_KEY=x \
//      uvicorn app.main:app --port 8095 &)
//   NODE_PATH=$(npm root -g) node tests/browser/library.test.js http://localhost:8095/
const { chromium } = require('playwright');

const URL = process.argv[2] || 'http://localhost:8095/';
let failed = 0;
const check = (name, ok, extra) => { console.log((ok ? 'PASS ' : 'FAIL ') + name + (ok ? '' : '  ' + (extra || ''))); if (!ok) failed++; };

(async () => {
  const b = await chromium.launch(); const p = await b.newPage({viewport: {width: 1250, height: 900}});
  const pageErrors = []; p.on('pageerror', e => pageErrors.push(e.message));

  // ---------- stubs ----------
  let chatCalls = [], agentCalls = [], rules = {block: /system prompt|banana/i}, failNext = false;
  await p.route('**/api/chat', async route => {
    const body = JSON.parse(route.request().postData()); chatCalls.push(body);
    if (failNext) { failNext = false; return route.fulfill({status: 500, contentType: 'application/json', body: JSON.stringify({detail: 'gateway exploded'})}); }
    const last = body.messages[body.messages.length - 1].content;
    const blocked = rules.block.test(last);
    route.fulfill({status: 200, contentType: 'application/json', body: JSON.stringify({
      reply: blocked ? 'Your message was blocked by the Cisco AI Defense gateway.' : 'answer to: ' + last.slice(0, 25),
      blocked, route: 'protected', mode: 'gateway', request_id: 'abcdef1234567890', files: [], prompt_inspection: null, response_inspection: null,
      gateway_verdict: blocked ? {stage: 'prompt', rules: ['Prompt Injection'], verdict: 'This request violates rules: Prompt Injection'} : null})});
  });
  await p.route('**/api/agent/run', async route => {
    const body = JSON.parse(route.request().postData()); agentCalls.push(body);
    const ev = [{type: 'run_started', run_id: 'r1234567890', route: 'protected', model: 'claude-agent', files: [], mcp_inspection: 'monitor'},
                {type: 'final', reply: 'agent answer ' + agentCalls.length, stats: {llm_calls: 1, tool_calls: 0, mcp_inspections: 0, blocked: 0}}];
    route.fulfill({status: 200, contentType: 'text/event-stream', body: ev.map(e => 'data: ' + JSON.stringify(e) + '\n\n').join('')});
  });

  await p.goto(URL); await p.waitForFunction(() => document.getElementById('ver').textContent.length > 1 && document.getElementById('library').style.display === 'flex');
  const lib = await p.evaluate(() => fetch('/api/prompts').then(r => r.json()));
  const idle = () => p.waitForFunction(() => !document.getElementById('send').disabled && !document.querySelector('#scenario button[disabled]:not(:last-child)') || true);
  const pick = async (cat, id) => { await p.selectOption('#libCat', cat); await p.selectOption('#libItem', id); };
  const scenarioText = () => p.$eval('#scenario', e => e.innerText.replace(/\n+/g, ' | '));
  const waitScenario = (re) => p.waitForFunction(r => new RegExp(r).test(document.getElementById('scenario').innerText), re.source, {timeout: 20000});

  // ---------- the dropdowns ----------
  const cats = await p.$$eval('#libCat option', o => o.map(x => x.textContent));
  check('category list: placeholder + 5 categories with counts', cats.length === 6 && cats[1].startsWith('Harmless (') && cats[5].startsWith('Multi-turn ('), JSON.stringify(cats));
  check('prompt list is disabled until a category is chosen', await p.$eval('#libItem', e => e.disabled));
  for (const c of lib.categories) {
    await p.selectOption('#libCat', c.id);
    const n = await p.$$eval('#libItem option', o => o.length - 1), groups = await p.$$eval('#libItem optgroup', g => g.length);
    const expected = lib.items.filter(i => i.category === c.id).length;
    check('category ' + c.id + ': ' + expected + ' prompts in groups', n === expected && groups >= 2 && !(await p.$eval('#libItem', e => e.disabled)), n + ' options, ' + groups + ' groups');
  }
  const colors = await p.$eval('#libCat', e => e.style.borderColor);
  check('the chosen category colours the lists', colors !== '', colors);

  // ---------- single prompts ----------
  const harmless = lib.items.find(i => i.category === 'harmless' && i.mode === 'any');
  await pick('harmless', harmless.id);
  check('picking a prompt fills the message box (nothing is sent)', (await p.inputValue('#q')) === harmless.prompt && chatCalls.length === 0);
  check('the note says what is typical and that the policy decides', (await p.textContent('#libInfo')).includes('Typical AI Defense result') && (await p.textContent('#libInfo')).includes('policy'));
  const email = lib.items.find(i => i.title.startsWith('Indirect: injection inside an email'));
  await pick('security', email.id);
  check('a prompt with line breaks is filled intact', (await p.inputValue('#q')) === email.prompt && email.prompt.includes('\n'));
  const agentItem = lib.items.find(i => i.category === 'security' && i.mode === 'agent');
  await pick('security', agentItem.id);
  check('a prompt that needs tools switches to Agent mode', await p.$eval('#mAgent', e => e.classList.contains('on')) && (await p.inputValue('#q')) === agentItem.prompt && (await p.textContent('#libInfo')).includes('Agent mode'));
  await p.click('#mChat');

  // ---------- multi-turn: step by step ----------
  const scen = lib.items.find(i => i.title === "'Max' has no rules");
  await p.fill('#q', 'leftover'); await p.click('#send'); await p.waitForFunction(() => !document.getElementById('send').disabled);
  const msgsBefore = await p.$$eval('.msg', m => m.length);
  await pick('multiturn', scen.id);
  check('choosing a scenario starts from a clean chat and shows the player', (await p.$$eval('.msg', m => m.length)) === 0 && msgsBefore > 0 && (await p.isVisible('#scenario')), await scenarioText());
  check('the player lists every turn and starts at turn 1', (await p.$$eval('#scenario li', l => l.length)) === 3 && (await scenarioText()).includes('turn 1 of 3'));
  chatCalls = [];
  await p.click('text=▶ Next turn'); await waitScenario(/turn 2 of 3/);
  check('Next turn sends exactly the first message', chatCalls.length === 1 && chatCalls[0].messages.length === 1 && chatCalls[0].messages[0].content === scen.turns[0]);
  await p.click('text=▶ Next turn'); await waitScenario(/finished|blocked|turn 3 of 3/);
  const second = chatCalls[1];
  check('the second turn carries the whole conversation (so AI Defense sees it)', chatCalls.length === 2 && second.messages.length === 3 && second.messages[1].role === 'assistant' && second.messages[2].content === scen.turns[1], JSON.stringify(second && second.messages.map(m => m.role)));
  await p.click('text=▶ Next turn'); await waitScenario(/Stopped: AI Defense blocked turn 3 of 3/);
  check('a blocked turn ends the scenario and is marked', (await p.$$eval('#scenario li.blocked', l => l.length)) === 1 && (await p.$$eval('#scenario li.done', l => l.length)) === 2);
  check('after the end the Next turn and Run all buttons are disabled', await p.$eval('#scenario', s => [...s.querySelectorAll('button')].filter(b => /Next turn|Run all/.test(b.textContent)).every(b => b.disabled)));
  await p.click('text=↺ Restart');
  check('Restart clears the chat and goes back to turn 1', (await scenarioText()).includes('turn 1 of 3') && (await p.$$eval('.msg', m => m.length)) === 0);

  // ---------- multi-turn: run all ----------
  const bali = lib.items.find(i => i.title.startsWith('Planning a trip'));
  await pick('multiturn', bali.id); chatCalls = [];
  await p.click('text=⏩ Run all'); await waitScenario(/All 3 turns were allowed/);
  check('Run all sends every turn in order and reports that all were allowed', chatCalls.length === 3 && chatCalls.map(c => c.messages[c.messages.length - 1].content).join('|') === bali.turns.join('|') && (await p.$$eval('#scenario li.done', l => l.length)) === 3);
  check('the answers of the model are in the chat', (await p.$$eval('.msg.bot', m => m.length)) === 3);

  // ---------- multi-turn: run all stops at the first block ----------
  const hypo = lib.items.find(i => i.title.startsWith('Hypothetical AI')); await pick('multiturn', hypo.id); chatCalls = [];
  await p.click('text=⏩ Run all'); await waitScenario(/Stopped: AI Defense blocked turn 3/);
  check('Run all stops at the blocked turn', chatCalls.length === 3 && (await p.$$eval('#scenario li.blocked', l => l.length)) === 1);

  // ---------- a failing turn can be retried ----------
  await pick('multiturn', bali.id); chatCalls = []; failNext = true;
  await p.click('text=▶ Next turn'); await waitScenario(/failed: gateway exploded/);
  check('a failed turn shows the error and keeps its place', (await scenarioText()).includes('turn 1 of 3') && (await p.$$eval('#scenario li.cur', l => l.length)) === 1);
  await p.click('text=▶ Next turn'); await waitScenario(/turn 2 of 3/);
  check('Next turn retries the failed turn', chatCalls.length === 2 && chatCalls[1].messages.length === 1);

  // ---------- an agent scenario runs in Agent mode ----------
  const chain = lib.items.find(i => i.category === 'multiturn' && i.mode === 'agent');
  await pick('multiturn', chain.id); agentCalls = [];
  check('an agent scenario switches to Agent mode', await p.$eval('#mAgent', e => e.classList.contains('on')));
  await p.click('text=⏩ Run all'); await waitScenario(/All 3 turns were allowed/);
  check('its turns go through the agent endpoint', agentCalls.length === 3 && agentCalls[2].messages.length === 5, agentCalls.length + ' calls');
  await p.click('text=✕ Close');
  check('Close hides the player', !(await p.isVisible('#scenario')));
  await p.click('#mChat');

  // ---------- every prompt of the library can be loaded through the real dropdowns ----------
  let loaded = 0, bad = [];
  for (const it of lib.items) {
    await p.selectOption('#libCat', it.category); await p.selectOption('#libItem', it.id);
    const ok = it.turns ? (await p.isVisible('#scenario')) && (await p.$$eval('#scenario li', l => l.length)) === it.turns.length
                        : (await p.inputValue('#q')) === it.prompt;
    if (ok) loaded++; else bad.push(it.id);
  }
  check('all ' + lib.items.length + ' prompts load correctly from the dropdowns', loaded === lib.items.length, bad.join(','));
  check('no page errors', pageErrors.length === 0, pageErrors.join('; '));

  await p.selectOption('#libCat', 'security'); await p.selectOption('#libItem', email.id);
  await p.screenshot({path: process.env.SHOT || 'library.png'});
  await b.close();
  console.log(failed ? `\n${failed} FAILED` : '\nall passed');
  process.exit(failed ? 1 : 0);
})().catch(e => { console.error('ERROR', e.message); process.exit(2); });
