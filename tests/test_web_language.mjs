import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import { test } from 'node:test';

const source = fs.readFileSync(new URL('../src/sn_proactive_agent/web/app.js', import.meta.url), 'utf8');
const html = fs.readFileSync(new URL('../src/sn_proactive_agent/web/index.html', import.meta.url), 'utf8');
const key = 'sn-proactive-agent:language';

function dashboard(languages, saved, unavailable = false) {
  const nodes = new Map();
  function node(id) {
    if (!nodes.has(id)) nodes.set(id, {
      dataset: {}, listeners: {}, textContent: '', innerHTML: '',
      addEventListener(type, fn) { this.listeners[type] = fn; },
      querySelectorAll() { return []; },
      setAttribute() {},
    });
    return nodes.get(id);
  }
  const storage = new Map(saved ? [[key, saved]] : []);
  const listeners = {};
  const context = vm.createContext({
    navigator: { languages, language: languages[0] },
    document: { documentElement: {}, activeElement: null,
      getElementById: node, querySelectorAll: () => [], querySelector: () => null,
      addEventListener() {},
    },
    window: { addEventListener(type, fn) { listeners[type] = fn; },
      localStorage: {
        getItem(k) { if (unavailable) throw Error('disabled'); return storage.get(k); },
        setItem(k, v) { if (unavailable) throw Error('disabled'); storage.set(k, v); },
        removeItem(k) { if (unavailable) throw Error('disabled'); storage.delete(k); },
      },
    },
    Intl, Date, Set, Map,
  });
  // Exercise the real page initialization and event handlers without polling a server.
  const instrumented = source.replace('  refresh();\n  window.setInterval(refresh, 2200);',
    '  globalThis.page = { state, translations, t, currentLanguage, renderProject, renderItemProgressBar, renderSuggestionCard, renderDailyReport, renderEvent };');
  assert.notEqual(instrumented, source);
  vm.runInContext(instrumented, context);
  return { ...context.page, context, storage, node, listeners,
    choose(value) { node('language-select').listeners.change({ target: { value } }); },
  };
}

test('Chinese locales select Chinese; other and missing languages fall back to English', () => {
  for (const locale of ['zh-CN', 'zh-TW', 'zh-HK', 'zh-Hans', 'zh']) {
    assert.equal(dashboard([locale]).currentLanguage(), 'zh');
  }
  for (const locale of ['en-US', 'ja-JP', 'fr-FR', 'de-DE', 'zhfake']) {
    assert.equal(dashboard([locale]).currentLanguage(), 'en');
  }
  assert.equal(dashboard([]).currentLanguage(), 'en');
  assert.equal(dashboard(['en-US', 'zh-TW']).currentLanguage(), 'zh');
});

test('manual preference persists, overrides detection, and can return to system default', () => {
  const page = dashboard(['zh-CN'], 'en');
  assert.equal(page.context.document.documentElement.lang, 'en');
  page.choose('zh');
  assert.equal(page.storage.get(key), 'zh');
  page.choose('en');
  assert.equal(page.t('suggestions_title'), 'Suggestions & decisions');
  page.choose('auto');
  assert.equal(page.storage.has(key), false);
  assert.equal(page.currentLanguage(), 'zh');
  assert.equal(dashboard(['zh-CN'], 'invalid').currentLanguage(), 'zh');
});

test('disabled storage does not prevent automatic or manual switching', () => {
  const page = dashboard(['zh-TW'], undefined, true);
  page.choose('en');
  assert.equal(page.currentLanguage(), 'en');
});

test('system language changes only affect automatic mode', () => {
  const page = dashboard(['en-US']);
  page.context.navigator.languages = ['zh-CN'];
  page.listeners.languagechange();
  assert.equal(page.context.document.documentElement.lang, 'zh-CN');
  page.choose('en');
  page.listeners.languagechange();
  assert.equal(page.context.document.documentElement.lang, 'en');
});

test('every static label and accessibility label has both translations', () => {
  const page = dashboard(['en-US']);
  assert.deepEqual(Object.keys(page.translations.en).sort(), Object.keys(page.translations.zh).sort());
  for (const [, key] of html.matchAll(/data-i18n(?:-aria-label)?="([^"]+)"/g)) {
    assert.ok(page.translations.en[key], key);
    assert.ok(page.translations.zh[key], key);
  }
  assert.match(html, /<details class="language-control"/);
  assert.match(html, /<select id="language-select"/);
});

test('cards translate controls while preserving original user content', () => {
  const page = dashboard(['en-US']);
  const item = { id: 'i', name: '周报', status: 'in_progress', event_count: 2 };
  const project = { id: 'p', name: '产品汇报', status: 'active', items: [item] };
  const rendered = page.renderProject(project);
  assert.match(rendered, /产品汇报/);
  assert.match(rendered, /周报/);
  assert.match(rendered, /View 2 events/);
  assert.match(rendered, /In progress/);
  assert.doesNotMatch(rendered, /下一步|事项|项目|查看|阻塞/);
  const suggestion = page.renderSuggestionCard({ title: '更新周报', status: 'pending' });
  assert.match(suggestion, /Accept &amp; continue/);
  assert.match(suggestion, /更新周报/);
  page.state.expandedProjects.add('p');
  page.state.expandedItems.add('p/i');
  page.state.pendingResponses.add('s');
  page.choose('zh');
  assert.ok(page.state.expandedProjects.has('p'));
  assert.ok(page.state.expandedItems.has('p/i'));
  assert.ok(page.state.pendingResponses.has('s'));
  assert.match(page.renderItemProgressBar(item), /进行中/);
});

test('language switching preserves connection error and last-updated state', () => {
  const page = dashboard(['en-US']);
  page.state.connectionFailed = true;
  page.choose('zh');
  assert.equal(page.node('last-updated').textContent, '服务连接失败');
  assert.match(page.node('projects-content').innerHTML, /请确认服务已启动/);
  page.state.connectionFailed = false;
  page.state.loaded = true;
  page.state.generatedAt = '2026-09-11T06:00:00Z';
  page.choose('en');
  assert.match(page.node('last-updated').textContent, /^Updated /);
});
