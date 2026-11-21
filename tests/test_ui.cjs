const assert = require('assert');
const fsnode = require('fs');
const vm = require('vm');
let writes = [], commands = [], notifications = [];
const fs = {
  read: () => Promise.resolve('{}'),
  write: (p, v) => { writes.push([p, v]); return Promise.resolve(); },
  exec: (p, args) => { commands.push([p, args]); return Promise.resolve({code:0,stdout:'running'}); }
};
const ui = {addNotification: (a,b,c) => notifications.push([b,c])};
const view = {extend: x => x};
function E(tag, attrs, children) {
  if (Array.isArray(attrs)) { children=attrs; attrs={}; }
  return {tag, attrs:attrs || {}, children:children || [], value:attrs && attrs.value || ''};
}
const source = fsnode.readFileSync('files/luci.js','utf8');
const app = vm.runInNewContext('(function(view,fs,ui,E){' + source + '\n})')(view,fs,ui,E);
const config = {port:'/dev/ttyUSB2',poll_seconds:30,corp_id:'id',corp_secret:'secret',agent_id:'1000002',to_user:'alice',extra:'preserve'};
const root = app.render(config);
function flatten(node) { return node && typeof node === 'object' ? [node,...(node.children || []).flatMap(flatten)] : []; }
const elements = flatten(root);
const buttons = elements.filter(x => x.tag === 'button');
assert.equal(elements.find(x => x.tag === 'input' && x.attrs.type === 'password').value, 'secret');
buttons[0].attrs.click();
setImmediate(() => {
  assert.equal(writes.length,1);
  assert.deepEqual(JSON.parse(writes[0][1]),config);
  assert.equal(commands[0][1][0], 'start');
  buttons[1].attrs.click();
  setImmediate(() => { assert.equal(commands[1][1][0],'stop'); console.log('LuCI settings and service actions: ok'); });
});
