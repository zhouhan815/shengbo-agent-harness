(function (global) {
  'use strict';

  function make(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text) element.textContent = text;
    return element;
  }

  async function start() {
    const api = global.ShengboWorkbenchApi;
    const header = document.querySelector('.wb-header-end');
    if (!api || !header) return;
    let reply;
    try {
      reply = await api.request('/api/assistant/v2/model-key');
    } catch (error) {
      return;
    }
    if (!['missing', 'placeholder'].includes(reply.status)) return;

    const open = make('button', 'wb-key-open', '连接 DeepSeek');
    open.type = 'button';
    open.setAttribute('aria-haspopup', 'dialog');
    header.prepend(open);
    const dialog = make('dialog', 'wb-key-dialog');
    dialog.setAttribute('aria-labelledby', 'wb-key-title');
    const title = make('h2', '', '连接 DeepSeek');
    title.id = 'wb-key-title';
    const description = make('p', 'wb-key-description', '请输入你的 DeepSeek API 密钥。点击连接后验证，验证通过才保存到这台电脑。');
    const form = make('form', 'wb-key-form');
    const label = make('label', 'wb-key-label', 'API 密钥');
    const key = make('input', 'wb-key-input');
    key.type = 'password';
    key.required = true;
    key.minLength = 12;
    key.maxLength = 512;
    key.autocomplete = 'off';
    key.spellcheck = false;
    key.setAttribute('autocapitalize', 'off');
    key.autofocus = true;
    label.append(key);
    const status = make('p', 'wb-key-status');
    status.setAttribute('role', 'status');
    status.hidden = true;
    const actions = make('div', 'wb-key-actions');
    const later = make('button', 'wb-key-close', '稍后配置');
    later.type = 'button';
    const connect = make('button', 'wb-key-connect', '验证并保存');
    connect.type = 'submit';
    actions.append(later, connect);
    form.append(label, status, actions);
    dialog.append(title, description, form);
    document.body.append(dialog);
    let busy = false;

    function show() {
      if (!dialog.open) dialog.showModal();
      key.focus();
    }
    open.addEventListener('click', show);
    later.addEventListener('click', () => dialog.close());
    dialog.addEventListener('close', () => {
      key.value = '';
      if (open.isConnected) open.focus();
    });
    form.addEventListener('submit', async event => {
      event.preventDefault();
      if (busy || !form.reportValidity()) return;
      busy = true;
      const submitted = key.value.trim();
      key.value = '';
      key.disabled = true;
      connect.disabled = true;
      status.textContent = '正在验证密钥…';
      status.dataset.kind = 'pending';
      status.hidden = false;
      try {
        const result = await api.request('/api/assistant/v2/model-key/connect', {api_key: submitted}, {timeout: 15000});
        if (result.status === 'configured') {
          dialog.close();
          dialog.remove();
          open.remove();
        }
      } catch (error) {
        status.textContent = error.message || '密钥连接失败，请检查后重新输入。';
        status.dataset.kind = 'error';
      } finally {
        busy = false;
        key.disabled = false;
        connect.disabled = false;
        if (dialog.open) key.focus();
      }
    });
    show();
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start, {once: true});
  else start();
})(window);
