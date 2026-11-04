(function (global) {
  'use strict';
  const statusLabels = { ended:'已结束', active:'声播进行中', interrupted:'已中断', end_unknown:'结束时间未记录' };
  const eventLabels = { comment:'评论', like:'点赞', enter:'进场', gift:'礼物', social:'社交互动',
    audience_snapshot:'人数指标', stats_display:'平台统计' };
  const visibleDate = new Intl.DateTimeFormat('zh-CN', {
    timeZone:'Asia/Shanghai', year:'numeric', month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit', hourCycle:'h23'
  });
  function time(value) {
    const date = new Date(value || '');
    return Number.isFinite(date.getTime()) ? visibleDate.format(date) : '未记录';
  }
  function E(tag, text, className) {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (className) node.className = className;
    return node;
  }
  function button(label, handler) {
    const node = E('button', label, 'wb-button');
    node.type = 'button';
    node.addEventListener('click', handler);
    return node;
  }
  function sourceTitle(source) {
    return (source.purpose === 'external_test' ? '公开直播间测试' : '抖音直播间') + ' · ' + source.external_room_id;
  }
  function mount({ container, api, getContext }) {
    let roomId, opener, opened = 0, detailRequest = 0, selected, listRows = new Map();
    const dialog = E('dialog', undefined, 'wb-room-overlay wb-live-history');
    dialog.setAttribute('aria-labelledby','wb-live-history-title');
    const header = E('header', undefined, 'wb-room-overlay-head');
    const title = E('h2','直播历史');
    title.id = 'wb-live-history-title';
    const closeButton = button('关闭', () => close());
    header.append(title, closeButton);
    const body = E('div', undefined, 'wb-live-history-body');
    const sidebar = E('section', undefined, 'wb-live-history-sessions');
    sidebar.setAttribute('aria-label','历史场次');
    const list = E('ol',undefined,'wb-live-history-list');
    const listFeedback = E('p',undefined,'wb-muted');
    listFeedback.setAttribute('role','status');
    const listMore = button('加载更早场次', () => loadList(listMore._cursor));
    listMore.hidden = true;
    sidebar.append(E('h3','历史场次'), list, listFeedback, listMore);
    const detail = E('section',undefined,'wb-live-history-detail');
    detail.setAttribute('aria-label','场次详情');
    body.append(sidebar,detail);
    dialog.append(header,body);
    container.append(dialog);

    function close({restoreFocus=true} = {}) {
      if (!dialog.open) return;
      opened++; detailRequest++;
      dialog.close();
      if (restoreFocus && opener?.isConnected) opener.focus({preventScroll:true});
      opener = null;
    }
    dialog.addEventListener('cancel', event => { event.preventDefault(); close(); });
    function sessionPath(id) {
      const path = '/api/rooms/' + encodeURIComponent(roomId) + '/live-history';
      return id ? path + '/' + encodeURIComponent(id) : path;
    }
    function showSources(sources) {
      const block = E('section',undefined,'wb-live-history-sources');
      block.append(E('h3','平台指标'));
      if (!sources.length) block.append(E('p','这场声播没有可对应的平台采集记录。','wb-muted'));
      for (const source of sources) {
        const panel = E('article',undefined,'wb-live-history-source');
        panel.append(E('h4',sourceTitle(source)));
        const counts = Object.entries(source.counts || {}).filter(([key]) => !['audience_snapshot','stats_display'].includes(key))
          .map(([key,count]) => (eventLabels[key] || key) + ' ' + count + ' 条');
        if (counts.length) panel.append(E('p',counts.join(' · '),'wb-muted'));
        const metrics = E('dl',undefined,'wb-live-history-metrics');
        for (const [key, name] of [['online','在线人数（候选）'],['total_visits','累计观看（候选）'],['likes_total','累计点赞']]) {
          const item = source.metrics?.[key];
          const group = E('div');
          const label = E('dt',name);
          const value = E('dd',item?.value ?? item?.raw ?? '—');
          if (item?.basis) {
            value.title = item.basis;
            group.append(label,value,E('small',time(item.updated_at)));
          } else group.append(label,value);
          metrics.append(group);
        }
        panel.append(metrics);
        block.append(panel);
      }
      return block;
    }
    function makeEvent(event) {
      const row = E('li');
      const meta = E('small',time(event.time) + ' · ' + (eventLabels[event.type] || '平台事件') + ' · ' + sourceTitle(event));
      const count = event.count == null ? '' : ' × ' + event.count;
      const message = event.text || ((eventLabels[event.type] || '平台事件') + count);
      const sender = ['audience_snapshot','stats_display'].includes(event.type) ? '' : event.nickname;
      row.append(meta,E('p',[sender,message].filter(Boolean).join(' · ')));
      return row;
    }
    async function loadDetail(id, before = null) {
      const token = ++detailRequest;
      selected = id;
      for (const [key, row] of listRows) row.setAttribute('aria-current',key === id ? 'true' : 'false');
      if (before === null) detail.replaceChildren(E('p','正在读取场次…','wb-muted'));
      const path = sessionPath(id) + (before === null ? '' : '?before=' + encodeURIComponent(before));
      try {
        const value = await api.request(path);
        if (!dialog.open || token !== detailRequest) return;
        let events;
        if (before === null) {
          const session = value.session;
          const top = E('header',undefined,'wb-live-history-detail-title');
          top.append(E('h3',time(session.started_at)),E('span',statusLabels[session.state] || session.state,'wb-badge'));
          const end = E('p',session.ended_at ? '结束于 ' + time(session.ended_at) : '本地声播开播时间','wb-muted');
          const sources = showSources(value.sources);
          events = E('ol',undefined,'wb-live-history-events');
          detail.replaceChildren(top,end,sources,E('h3','平台事件'),events);
          if (!value.events.length) detail.append(E('p','本场暂无可对应的平台事件。','wb-muted'));
        } else {
          events = detail.querySelector('.wb-live-history-events');
          detail.querySelector('[data-history-more-events]')?.remove();
          detail.querySelector('[data-history-event-error]')?.remove();
        }
        for (const event of value.events) events.append(makeEvent(event));
        if (value.next_before != null) {
          const more = button('加载更早事件', async () => {
            more.disabled = true;
            try { await loadDetail(id,value.next_before); } finally { more.disabled = false; }
          });
          more.dataset.historyMoreEvents = '';
          detail.append(more);
        }
      } catch (error) {
        if (!dialog.open || token !== detailRequest) return;
        const feedback = E('p',error.message || '场次暂时读取失败。','wb-inline-alert');
        feedback.setAttribute('role','alert');
        if (before !== null) {
          detail.querySelector('[data-history-event-error]')?.remove();
          feedback.dataset.historyEventError = '';
          detail.append(feedback);
        } else detail.replaceChildren(feedback,button('重新读取场次',() => loadDetail(id)));
      }
    }
    async function loadList(cursor = 0) {
      const token = opened;
      listMore.disabled = true;
      listFeedback.textContent = '正在读取场次…';
      try {
        const result = await api.request(sessionPath() + (cursor ? '?cursor=' + encodeURIComponent(cursor) : ''));
        if (!dialog.open || token !== opened) return;
        listFeedback.textContent = result.items.length ? '' : '这间直播间还没有已记录的开播场次。';
        for (const item of result.items) {
          if (listRows.has(item.id)) continue;
          const row = E('li');
          const action = button('',() => loadDetail(item.id));
          action.append(E('strong',time(item.started_at)),E('small',statusLabels[item.state] || item.state));
          listRows.set(item.id,action);
          row.append(action); list.append(row);
        }
        listMore._cursor = result.next_cursor;
        listMore.textContent = '加载更早场次';
        listMore.hidden = result.next_cursor == null;
        if (!selected && result.items[0]) await loadDetail(result.items[0].id);
        else if (!selected) detail.replaceChildren(E('p','完成一次声播播出后，可在这里按场次查看平台指标和事件。','wb-muted'));
      } catch (error) {
        if (!dialog.open || token !== opened) return;
        listFeedback.textContent = error.message || '暂时无法读取直播历史。';
        listMore._cursor = cursor;
        listMore.textContent = '重新读取';
        listMore.hidden = false;
      } finally {
        if (token === opened) listMore.disabled = false;
      }
    }
    function open({opener:button = document.activeElement} = {}) {
      if (dialog.open) return;
      roomId = getContext()?.room_id;
      if (!roomId) return;
      opener = button;
      opened++; selected = null; listRows = new Map();
      list.replaceChildren(); detail.replaceChildren(); listMore.hidden = true;
      dialog.showModal();
      closeButton.focus({preventScroll:true});
      return loadList();
    }
    return {open, close, isOpen: () => dialog.open};
  }
  global.ShengboRoomLiveHistory = {mount};
})(window);
