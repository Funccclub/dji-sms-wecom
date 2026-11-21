const assert = require('assert');
const fsnode = require('fs');
const vm = require('vm');
let writes = [], commands = [], notifications = [];
const pages = {
  latest: {
    records: [
      { sender: '123', time: '2026-11-21T20:28:03+08:00', body: '<b>短信</b>', part: 1, total: 2, status: 'sent' },
      { sender: '456', time: null, body: '测试', part: 1, total: 1, status: 'pending' }
    ],
    next_cursor: '25'
  },
  25: { records: [{ sender: '789', time: null, body: '旧短信', status: 'unknown' }], next_cursor: null }
};
let historyError = false;
const fs = {
  read: () => Promise.resolve('{}'),
  write: (p, v) => { writes.push([p, v]); return Promise.resolve(); },
  exec: (p, args) => {
    commands.push([p, args]);
    if (args[0] === 'history')
      return Promise.resolve(historyError ? {code:1} : {code:0,stdout:JSON.stringify(pages[args[1]])});
    return Promise.resolve({code:0,stdout:'running'});
  }
};
const ui = {addNotification: (a,b,c) => notifications.push([b,c])};
const view = {extend: x => x};
function E(tag, attrs, children) {
  if (Array.isArray(attrs)) { children=attrs; attrs={}; }
  return {
    tag, attrs:attrs || {}, children:children || [], value:attrs && attrs.value || '',
    replaceChildren(...children) { this.children = children; }
  };
}
const source = fsnode.readFileSync('files/luci.js','utf8');
const app = vm.runInNewContext('(function(view,fs,ui,E){' + source + '\n})')(view,fs,ui,E);
const config = {port:'/dev/ttyUSB2',poll_seconds:30,corp_id:'id',corp_secret:'secret',agent_id:'1000002',to_user:'alice',extra:'preserve'};
const root = app.render(config);
function flatten(node) { return node && typeof node === 'object' ? [node,...(node.children || []).flatMap(flatten)] : []; }
const elements = flatten(root);
const buttons = elements.filter(x => x.tag === 'button');
const button = label => buttons.find(x => x.children[0] === label);
const cells = () => flatten(root).filter(x => x.tag === 'td').map(x => x.children[0]);
const settle = () => new Promise(resolve => setImmediate(resolve));

async function test() {
  await settle();
  assert.equal(commands[0][1].join(' '), 'history latest');
  assert.ok(cells().includes('<b>短信</b>'));
  assert.ok(cells().includes('已推送'));
  assert.ok(cells().includes('未推送'));
  assert.ok(flatten(root).some(x => x.children.includes('第 1/2 段')));
  assert.ok(button('较新记录').disabled);
  assert.ok(!button('较早记录').disabled);

  button('较早记录').attrs.click();
  await settle();
  assert.equal(commands[1][1].join(' '), 'history 25');
  assert.ok(cells().includes('历史状态未知'));
  assert.ok(button('较早记录').disabled);
  button('较新记录').attrs.click();
  await settle();
  assert.ok(cells().includes('已推送'));

  historyError = true;
  button('刷新记录').attrs.click();
  await settle();
  assert.match(elements.find(x => x.attrs.role === 'status').textContent, /无法读取/);
  assert.ok(cells().includes('已推送'));

  assert.equal(elements.find(x => x.tag === 'input' && x.attrs.type === 'password').value, 'secret');
  button('保存并启动').attrs.click();
  await settle();
  assert.equal(writes.length, 1);
  assert.deepEqual(JSON.parse(writes[0][1]), config);
  assert.equal(commands.at(-1)[1][0], 'start');
  button('停止服务').attrs.click();
  await settle();
  assert.equal(commands.at(-1)[1][0], 'stop');
  console.log('LuCI settings, service actions, and SMS history: ok');
}

test().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
