'use strict';
'require view';
'require fs';
'require ui';

var configPath = '/etc/dji-sms-wecom.json';
var controlPath = '/usr/libexec/dji-sms-wecom-control';

return view.extend({
  load: function() {
    return fs.read(configPath).then(function(text) { return JSON.parse(text); });
  },
  render: function(config) {
    var fields = {};
    function field(key, label, type, placeholder, explanation) {
      var input = E('input', { 'class': 'cbi-input-text', 'type': type || 'text',
        'value': String(config[key] == null ? '' : config[key]), 'placeholder': placeholder || '' });
      fields[key] = input;
      return E('div', { 'class': 'cbi-value' }, [
        E('label', { 'class': 'cbi-value-title' }, [label]),
        E('div', { 'class': 'cbi-value-field' }, [input,
          E('div', { 'class': 'cbi-value-description' }, [explanation || ''])])
      ]);
    }
    function notify(message, error) {
      ui.addNotification(null, E('p', {}, [message]), error ? 'danger' : 'info');
    }
    function run(action) {
      return fs.exec(controlPath, [action]).then(function(res) {
        if (res.code !== 0) throw new Error(res.stderr || '操作失败，退出码 ' + res.code);
        return res;
      });
    }
    var save = E('button', { 'class': 'btn cbi-button cbi-button-action', 'type': 'button',
      'click': function() {
        var next = Object.assign({}, config);
        Object.keys(fields).forEach(function(k) { next[k] = fields[k].value.trim(); });
        if (!/^\/dev\/[a-zA-Z0-9_./-]+$/.test(next.port) ||
            !next.corp_id || !next.corp_secret || !/^\d+$/.test(next.agent_id) || !next.to_user ||
            !/^\d+$/.test(next.poll_seconds) || Number(next.poll_seconds) < 10) {
          notify('请填写全部字段：串口须以 /dev/ 开头，AgentId 和轮询间隔须为数字，间隔至少 10 秒。', true);
          return;
        }
        next.poll_seconds = Number(next.poll_seconds);
        save.disabled = true;
        fs.write(configPath, JSON.stringify(next, null, 2) + '\n').then(function() {
          config = next;
          return run('start');
        }).then(function() {
          notify('配置已保存，服务已启动并设置开机启动。请发送新短信测试。', false);
        }).catch(function(e) { notify('保存或启动失败：' + e.message, true); })
          .finally(function() { save.disabled = false; });
      }
    }, ['保存并启动']);
    var stop = E('button', { 'class': 'btn cbi-button cbi-button-negative', 'type': 'button',
      'click': function() {
        run('stop').then(function() { notify('服务已停止，开机启动已关闭。', false); })
          .catch(function(e) { notify('停止失败：' + e.message, true); });
      }
    }, ['停止服务']);
    var status = E('button', { 'class': 'btn cbi-button', 'type': 'button',
      'click': function() {
        fs.exec(controlPath, ['status']).then(function(r) {
          notify('服务状态：' + (r.stdout || r.stderr || (r.code === 0 ? '运行中' : '未运行')).trim(), false);
        }).catch(function(e) { notify('无法读取状态：' + e.message, true); });
      }
    }, ['查看状态']);

    var history = E('div', { 'style': 'overflow-x: auto' });
    var historyNotice = E('p', { 'role': 'status' });
    var historyPages = [null];
    var nextCursor = null;
    var historyPending = false;
    var deliveryLabels = {
      pending: '未推送',
      sent: '已推送',
      baseline: '首次读取未推送',
      unknown: '历史状态未知'
    };
    function historyButton(label, onClick) {
      return E('button', { 'class': 'btn cbi-button', 'type': 'button', 'click': onClick }, [label]);
    }
    var refreshHistory = historyButton('刷新记录', function() { loadHistory([null]); });
    var newerHistory = historyButton('较新记录', function() {
      if (historyPages.length > 1) loadHistory(historyPages.slice(0, -1));
    });
    var olderHistory = historyButton('较早记录', function() {
      if (nextCursor) loadHistory(historyPages.concat(nextCursor));
    });
    function updateHistoryButtons() {
      refreshHistory.disabled = historyPending;
      newerHistory.disabled = historyPending || historyPages.length === 1;
      olderHistory.disabled = historyPending || !nextCursor;
    }
    function renderHistory(records) {
      if (!records.length) {
        history.replaceChildren();
        historyNotice.textContent = '暂无短信记录。';
        return;
      }
      var headings = ['短信时间', '发件人', '短信内容', '推送状态'].map(function(label) {
        return E('th', { 'scope': 'col' }, [label]);
      });
      var rows = records.map(function(record) {
        var time = record.time ? record.time.slice(0, 19).replace('T', ' ') + ' ' + record.time.slice(19) : '—';
        var content = [record.body];
        if (record.total > 1) {
          content.push(E('div', { 'class': 'cbi-value-description' },
            ['第 ' + record.part + '/' + record.total + ' 段']));
        }
        return E('tr', {}, [
          E('td', {}, [time]),
          E('td', {}, [record.sender]),
          E('td', { 'style': 'white-space: pre-wrap; overflow-wrap: anywhere' }, content),
          E('td', {}, [deliveryLabels[record.status] || deliveryLabels.unknown])
        ]);
      });
      history.replaceChildren(E('table', { 'class': 'table' }, [
        E('thead', {}, [E('tr', {}, headings)]),
        E('tbody', {}, rows)
      ]));
      historyNotice.textContent = '';
    }
    function loadHistory(pages) {
      if (historyPending) return;
      historyPending = true;
      updateHistoryButtons();
      historyNotice.textContent = '正在读取短信记录…';
      return fs.exec(controlPath, ['history', pages[pages.length - 1] || 'latest']).then(function(res) {
        if (res.code !== 0) throw new Error('history command failed');
        var page = JSON.parse(res.stdout);
        renderHistory(page.records);
        historyPages = pages;
        nextCursor = page.next_cursor;
      }).catch(function() {
        historyNotice.textContent = '无法读取短信记录。请刷新记录后重试。';
      }).finally(function() {
        historyPending = false;
        updateHistoryButtons();
      });
    }
    loadHistory(historyPages);

    var recipient = E('input', { 'class': 'cbi-input-text', 'type': 'tel',
      'id': 'sms-recipient', 'style': 'width: 100%; max-width: 42rem; box-sizing: border-box',
      'placeholder': '手机号码，国际号码以 + 开头' });
    var smsContent = E('textarea', { 'class': 'cbi-input-text', 'rows': 6,
      'id': 'sms-content', 'style': 'width: 100%; max-width: 42rem; min-height: 9rem; box-sizing: border-box; resize: vertical',
      'placeholder': '短信内容' });
    var smsResult = E('p', { 'role': 'status' });
    var sendSms = E('button', { 'class': 'btn cbi-button cbi-button-action', 'type': 'button',
      'click': function() {
        var number = recipient.value.trim();
        var content = smsContent.value;
        if (!/^\+?\d{1,20}$/.test(number) || !content.trim() || content.length > 70) {
          smsResult.textContent = '请输入有效号码和短信内容，内容最多 70 个字符。';
          return;
        }
        var bytes = new Uint8Array(16);
        crypto.getRandomValues(bytes);
        var job = Array.from(bytes, function(b) { return b.toString(16).padStart(2, '0'); }).join('');
        var encoded = btoa(unescape(encodeURIComponent(content)));
        var deadline = Date.now() + 180000;
        sendSms.disabled = true;
        smsResult.textContent = '正在提交短信…';
        function check() {
          fs.exec(controlPath, ['send-status', job]).then(function(res) {
            if (res.code !== 0) throw new Error();
            var state = JSON.parse(res.stdout);
            if (state.status === 'sent' || state.status === 'failed') {
              smsResult.textContent = state.message || (state.status === 'sent' ? '模块已接受短信。' : '发送失败。');
              sendSms.disabled = false;
              if (state.status === 'sent') smsContent.value = '';
              loadOutgoing([null]);
            } else if (Date.now() < deadline) {
              smsResult.textContent = '正在等待模块处理…';
              setTimeout(check, 2000);
            } else {
              smsResult.textContent = '等待超时，发送结果未知，请先确认收件情况。';
              sendSms.disabled = false;
            }
          }).catch(function() {
            if (Date.now() < deadline) setTimeout(check, 2000);
            else {
              smsResult.textContent = '无法查询发送结果，请先确认收件情况。';
              sendSms.disabled = false;
            }
          });
        }
        fs.exec(controlPath, ['submit-sms', job, number, encoded]).then(function(res) {
          if (res.code !== 0) {
            smsResult.textContent = res.stderr || '提交失败。';
            sendSms.disabled = false;
          } else check();
        }).catch(function() { check(); });
      }
    }, ['发送短信']);

    var outgoing = E('div', { 'style': 'overflow-x: auto' });
    var outgoingNotice = E('p', { 'role': 'status' });
    var outgoingPages = [null];
    var outgoingNext = null;
    var outgoingPending = false;
    var outgoingLabels = { queued: '等待发送', sending: '发送中', sent: '模块已接受', failed: '发送失败' };
    var refreshOutgoing = historyButton('刷新发送记录', function() { loadOutgoing([null]); });
    var newerOutgoing = historyButton('较新发送记录', function() {
      if (outgoingPages.length > 1) loadOutgoing(outgoingPages.slice(0, -1));
    });
    var olderOutgoing = historyButton('较早发送记录', function() {
      if (outgoingNext) loadOutgoing(outgoingPages.concat(outgoingNext));
    });
    function updateOutgoingButtons() {
      refreshOutgoing.disabled = outgoingPending;
      newerOutgoing.disabled = outgoingPending || outgoingPages.length === 1;
      olderOutgoing.disabled = outgoingPending || !outgoingNext;
    }
    function loadOutgoing(pages) {
      if (outgoingPending) return;
      outgoingPending = true;
      updateOutgoingButtons();
      outgoingNotice.textContent = '正在读取发送记录…';
      return fs.exec(controlPath, ['outgoing-history', pages[pages.length - 1] || 'latest']).then(function(res) {
        if (res.code !== 0) throw new Error();
        var page = JSON.parse(res.stdout);
        var headings = ['发送时间', '接收号码', '短信内容', '发送状态'].map(function(label) {
          return E('th', { 'scope': 'col' }, [label]);
        });
        var rows = page.records.map(function(record) {
          var time = record.time ? record.time.slice(0, 19).replace('T', ' ') : '—';
          var statusText = outgoingLabels[record.status] || '状态未知';
          if (record.status === 'failed' && record.message) statusText += '：' + record.message;
          return E('tr', {}, [
            E('td', {}, [time]), E('td', {}, [record.number]),
            E('td', { 'style': 'white-space: pre-wrap; overflow-wrap: anywhere' }, [record.body]),
            E('td', {}, [statusText])
          ]);
        });
        outgoing.replaceChildren(E('table', { 'class': 'table' }, [
          E('thead', {}, [E('tr', {}, headings)]), E('tbody', {}, rows)
        ]));
        outgoingNotice.textContent = rows.length ? '' : '暂无发送记录。';
        outgoingPages = pages;
        outgoingNext = page.next_cursor;
      }).catch(function() {
        outgoingNotice.textContent = '无法读取发送记录。请刷新后重试。';
      }).finally(function() {
        outgoingPending = false;
        updateOutgoingButtons();
      });
    }

    var settingsPanel = E('div', { 'class': 'cbi-section', 'id': 'sms-settings', 'role': 'tabpanel' }, [
      E('h3', {}, ['转发设置']),
      E('p', {}, ['首次启动时，已有短信作为基线，后续新短信会转发到企业微信。']),
      E('div', {}, [
        field('port', 'AT 串口', 'text', '/dev/ttyUSB2', '大疆模块的 AT 串口。'),
        field('poll_seconds', '轮询间隔（秒）', 'number', '30', '最短 10 秒。'),
        field('corp_id', '企业 ID', 'text', '', '企业微信管理后台的企业 ID。'),
        field('corp_secret', '应用 Secret', 'password', '', '自建应用的 Secret；保存在本机配置文件。'),
        field('agent_id', '应用 AgentId', 'text', '', '自建应用的数字 AgentId。'),
        field('to_user', '接收成员 UserID', 'text', '', '多个成员用 | 分隔；@all 会发送给应用可见范围内的所有成员。')
      ]),
      E('div', { 'class': 'cbi-page-actions' }, [save, ' ', stop, ' ', status])
    ]);
    var sendPanel = E('div', { 'class': 'cbi-section', 'id': 'sms-outgoing', 'role': 'tabpanel' }, [
        E('h3', {}, ['发送短信']),
        E('div', { 'class': 'cbi-value' }, [
          E('label', { 'class': 'cbi-value-title', 'for': 'sms-recipient' }, ['接收号码']),
          E('div', { 'class': 'cbi-value-field' }, [recipient])
        ]),
        E('div', { 'class': 'cbi-value' }, [
          E('label', { 'class': 'cbi-value-title', 'for': 'sms-content' }, ['短信内容']),
          E('div', { 'class': 'cbi-value-field' }, [smsContent])
        ]),
        E('div', { 'class': 'cbi-value' }, [
          E('div', { 'class': 'cbi-value-title' }),
          E('div', { 'class': 'cbi-value-field' }, [sendSms, smsResult])
        ]),
        E('h3', {}, ['发送记录']),
        E('p', {}, ['记录保留 7 天。']),
        E('p', {}, [refreshOutgoing, ' ', newerOutgoing, ' ', olderOutgoing]),
        outgoingNotice, outgoing
    ]);
    var receivePanel = E('div', { 'class': 'cbi-section', 'id': 'sms-incoming', 'role': 'tabpanel' }, [
        E('h3', {}, ['接收记录']),
        E('p', {}, ['短信记录保留 7 天。']),
        E('p', {}, [refreshHistory, ' ', newerHistory, ' ', olderHistory]),
        historyNotice,
        history
    ]);
    var panels = [receivePanel, sendPanel, settingsPanel];
    var tabs = [];
    var tabItems = [];
    function selectTab(index) {
      panels.forEach(function(panel, i) {
        panel.hidden = i !== index;
        tabItems[i].className = i === index ? 'tab' : 'tab-disabled';
        tabs[i].setAttribute('aria-selected', i === index ? 'true' : 'false');
      });
      if (index === 1) loadOutgoing([null]);
    }
    ['接收短信', '发送短信', '转发设置'].forEach(function(label, index) {
      var link = E('a', { 'href': '#', 'role': 'tab',
        'aria-controls': ['sms-incoming', 'sms-outgoing', 'sms-settings'][index],
        'click': function(event) { event.preventDefault(); selectTab(index); }
      }, [label]);
      tabs.push(link);
      tabItems.push(E('li', {}, [link]));
    });
    selectTab(0);
    return E('div', { 'class': 'cbi-map' }, [
      E('h2', {}, ['大疆短信']),
      E('ul', { 'class': 'cbi-tabmenu', 'role': 'tablist', 'aria-label': '短信功能' }, tabItems),
      receivePanel, sendPanel, settingsPanel
    ]);
  },
  handleSave: null,
  handleSaveApply: null,
  handleReset: null
});
