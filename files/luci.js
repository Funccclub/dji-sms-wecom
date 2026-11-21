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

    return E('div', { 'class': 'cbi-map' }, [
      E('h2', {}, ['大疆短信转发']),
      E('p', {}, ['通过大疆 4G 模块读取新短信并发送到企业微信自建应用。首次启动会跳过现有短信；不会自动删除短信。']),
      E('div', { 'class': 'cbi-section' }, [
        field('port', 'AT 串口', 'text', '/dev/ttyUSB2', '大疆模块的 AT 串口。'),
        field('poll_seconds', '轮询间隔（秒）', 'number', '30', '最短 10 秒。'),
        field('corp_id', '企业 ID', 'text', '', '企业微信管理后台的企业 ID。'),
        field('corp_secret', '应用 Secret', 'password', '', '自建应用的 Secret；保存在本机配置文件。'),
        field('agent_id', '应用 AgentId', 'text', '', '自建应用的数字 AgentId。'),
        field('to_user', '接收成员 UserID', 'text', '', '多个成员用 | 分隔；@all 会发送给应用可见范围内的所有成员。')
      ]),
      E('div', { 'class': 'cbi-page-actions' }, [save, ' ', stop, ' ', status]),
      E('div', { 'class': 'cbi-section' }, [
        E('h3', {}, ['短信记录']),
        E('p', {}, [refreshHistory, ' ', newerHistory, ' ', olderHistory]),
        historyNotice,
        history
      ])
    ]);
  },
  handleSave: null,
  handleSaveApply: null,
  handleReset: null
});
