/* User-facing projections of decision payloads. Identifiers and write bindings stay on the wire. */
(function (global) {
  'use strict';

  const TITLES = {
    catalog_confirm: '确认商品入库方案',
    catalog_intake_approval: '确认商品入库方案',
    catalog_intake_price: '补充商品日常售价',
    new_product_import: '确认导入新商品',
    product_identity: '确认要处理的商品',
    product_selection_required: '选择本场商品',
    task_scope: '确认任务范围',
    playlist_order: '确认播放顺序',
    script_audio_preview_approval: '确认话术试听',
    text_audio_approval: '确认整包话术与语音',
    selling_points_generation: '选择是否整理卖点',
    selling_points_confirmation: '确认商品卖点'
  };
  const OPENINGS = { opening_long: '长开场', opening_short: '短引入', merchant_original: '原稿开场' };
  const PROVIDERS = { system: '本机普通话', minimax: 'MiniMax' };
  const CURRENCIES = { CNY: '元人民币', TWD: '新台币', USD: '美元', AUD: '澳元' };
  const LEGACY_CATALOG_LABELS = {
    price: '日常售价', promo_price: '促销参考价', image: '商品图片', sales_platform: '销售平台', target_audience: '适用人群',
    storage_conditions: '储存条件', shelf_life: '保质期', ingredients: '配料'
  };
  const LEGACY_CATALOG_FIELD = /(?<![A-Za-z0-9_.])`?(price|promo_price|image|sales_platform|target_audience|storage_conditions|shelf_life|ingredients)`?(?![A-Za-z0-9_.])/g;
  const PRIVATE_REF = /\b(?:run|conversation|decision|preparation|prep|product|room|analysis|material|variant|script|script_revision|module|artifact|occurrence|approval|review|operation)_[0-9a-z][0-9a-z_-]{5,}\b/gi;
  const PRIVATE_HASH = /\b(?:sha256:)?[0-9a-f]{24,}\b/gi;
  const OPAQUE_OPTION = /^[a-z]+[_:][a-z0-9_:.-]+$/i;

  function text(value) {
    if (typeof value !== 'string') return '';
    return value
      // Historic confirmations used database field assignments in their prose.
      .replace(/(?:日常售价|日常价)\s*[=＝→]\s*[`]?price[`]?\s*[、，,；;]\s*(?:直播售价|直播价)\s*[=＝→]\s*[`]?promo_price[`]?/g, '日常售价和促销参考价')
      .replace(/(?:日常售价|日常价)\s*[=＝→]\s*[`]?price[`]?/g, '日常售价')
      .replace(/(?:直播售价|直播价)\s*[=＝→]\s*[`]?promo_price[`]?/g, '促销参考价')
      .replace(LEGACY_CATALOG_FIELD, (_, field) => LEGACY_CATALOG_LABELS[field])
      .replace('保质期/配料', '保质期和配料')
      .replace('【价格映射】', '【价格】')
      .replace('【SKU结构】', '【商品规格】')
      .replace(/拆分为(\d+)个SKU商品/g, '拆成$1条商品记录')
      .replace(/在售SKU/g, '在售规格')
      .replace(PRIVATE_REF, '（内部编号已省略）')
      .replace(PRIVATE_HASH, '（内部编号已省略）')
      .trim();
  }

  function title(question) {
    if (question.kind === 'task_scope' && question.context?.resolution_kind === 'room_selection') return '选择直播间';
    return Object.hasOwn(TITLES, question.kind) ? TITLES[question.kind] : (text(question.title) || '需要你确认');
  }

  function option(item) {
    const label = text(item?.label || item?.title);
    if (label && !OPAQUE_OPTION.test(label) && !/^(?:(?:选项|option)\s*)?\d+$/i.test(label)) return label;
    return '';
  }

  function preview(question) {
    const data = question.preview;
    if (!data || typeof data !== 'object') return null;
    let rows;
    let label;
    switch (question.kind) {
      case 'catalog_intake_price':
        label = '待补价格的商品';
        rows = [
          { label: '商品', text: data.product_name },
          { label: '规格编号', text: data.sku_id },
          { label: '币种', text: Object.hasOwn(CURRENCIES, data.currency) ? CURRENCIES[data.currency] : '' }
        ];
        break;
      case 'playlist_order':
        label = '完整播放顺序';
        rows = Array.isArray(data.items) ? data.items.map((item, i) => ({
          label: '第 ' + (i + 1) + ' 项',
          text: [text(item.product_name), item.version_number ? '第 ' + item.version_number + ' 版' : '', item.script_revision ? '修订 ' + item.script_revision : ''].filter(Boolean).join(' · ')
        })) : [];
        if (data.loop_label) rows.push({label: '循环方式', text: text(data.loop_label)});
        break;
      case 'text_audio_approval':
        label = '查看待批准的话术与声音';
        rows = [];
        if (data.voice) {
          const name = text(data.voice_display_name || data.voice.display_name);
          rows.push({label: '播报声音', text: [Object.hasOwn(PROVIDERS, data.voice.provider) ? PROVIDERS[data.voice.provider] : '', name || '本场选定的声音'].filter(Boolean).join(' · ')});
        }
        if (Number.isInteger(data.cost_scope?.characters)) rows.push({label: '待生成的文字量', text: data.cost_scope.characters + ' 字符'});
        if (Array.isArray(data.plan_summary)) data.plan_summary.forEach((item, i) => {
          if (Object.hasOwn(OPENINGS, item.opening_mode)) rows.push({label: '第 ' + (i + 1) + ' 个播放项', text: OPENINGS[item.opening_mode]});
        });
        if (Array.isArray(data.texts)) data.texts.forEach((item, i) => {
          // Approve the exact words sent to the voice service.
          if (typeof item.spoken_text === 'string' && item.spoken_text.trim()) rows.push({label: '待生成语音 ' + (i + 1), text: item.spoken_text});
        });
        break;
      case 'product_identity':
        label = '同名商品';
        rows = Array.isArray(data.candidates) ? data.candidates.map((item, i) => ({
          label: item.display_number ? '商品编号 ' + item.display_number : '候选商品 ' + (i + 1),
          text: [text(item.name || data.target_name), item.sku_id ? '规格编号 ' + item.sku_id : '', Number.isFinite(item.price) && Object.hasOwn(CURRENCIES, item.currency) ? '售价 ' + item.price + ' ' + CURRENCIES[item.currency] : ''].filter(Boolean).join(' · ')
        })) : [];
        break;
      case 'task_scope':
      case 'new_product_import':
      case 'product_selection_required':
        label = '拟执行的任务';
        rows = typeof data.summary === 'string' ? [{label: '任务内容', text: text(data.summary)}] : [];
        break;
      default:
        return null;
    }
    rows = rows.filter(row => row.text);
    return rows.length ? {title: label, rows} : null;
  }

  const api = { text, title, option, preview };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else global.ShengboDecisionCopy = api;
})(globalThis);

