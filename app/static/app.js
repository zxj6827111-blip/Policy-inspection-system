const $ = (selector) => document.querySelector(selector);

// ---------- 基础工具 ----------
function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>'"]/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[char]));
}
function validatedUrl(value) {
  try {
    const parsed = new URL(value, window.location.origin);
    return ['http:', 'https:'].includes(parsed.protocol) ? parsed.href : '#';
  } catch { return '#'; }
}
function safeUrl(value) { return escapeHtml(validatedUrl(value)); }
function fmtNum(value) { return Number(value ?? 0).toLocaleString('zh-CN'); }
function fmtTime(value) { return value ? new Date(value).toLocaleString('zh-CN', {hour12: false}) : '-'; }
function uniqueEvidenceTerms(values) {
  return [...new Set(values.map(value => String(value || '').trim()).filter(value => value.length >= 2))]
    .sort((left, right) => right.length - left.length);
}
function highlightEvidence(text, values) {
  const terms = uniqueEvidenceTerms(values);
  if (!terms.length) return escapeHtml(text || '');
  const expression = new RegExp(terms.map(term => term.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|'), 'g');
  const source = String(text || '');
  let cursor = 0;
  return source.replace(expression, (match, offset) => {
    const before = escapeHtml(source.slice(cursor, offset));
    cursor = offset + match.length;
    return `${before}<mark class="evidence-mark">${escapeHtml(match)}</mark>`;
  }) + escapeHtml(source.slice(cursor));
}
function cooldownText(value) {
  if (!value) return '-';
  const target = new Date(value);
  const seconds = Math.max(0, Math.ceil((target.getTime() - Date.now()) / 1000));
  if (seconds === 0) return `冷却已结束（${target.toLocaleString('zh-CN', {hour12:false})}）`;
  return `剩余 ${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒（至 ${target.toLocaleString('zh-CN', {hour12:false})}）`;
}
function toast(message) {
  const box = $('#toast'); box.textContent = message; box.style.display = 'block';
  clearTimeout(box._timer); box._timer = setTimeout(() => { box.style.display = 'none'; }, 4500);
}
let csrfToken = '';
async function api(url, options = {}) {
  const headers = {'Content-Type': 'application/json', ...(options.headers || {})};
  if (csrfToken && options.method && !['GET','HEAD'].includes(String(options.method).toUpperCase())) {
    headers['X-CSRF-Token'] = csrfToken;
  }
  const response = await fetch(url, {...options, headers, credentials: 'same-origin'});
  if (!response.ok) { const data = await response.json().catch(() => ({})); throw new Error(data.detail || `请求失败 ${response.status}`); }
  return response.json();
}

// ---------- 文案 ----------
const statusNames = {pending:'等待启动', running:'扫描中', paused:'等待处理', partial:'本批完成', cooling:'安全冷却中', completed:'已完成', stopped:'已停止', failed:'扫描失败'};
const phaseNames = {
  idle: '待命', discovering: '正在建立列表快照', processing: '正在检查详情',
  catching_up: '正在追赶新增内容', incremental_discovery: '正在执行增量发现',
  full_reconcile: '正在执行完整列表对账', cooling: '安全冷却', review: '等待人工复核',
};
function phaseLabel(job) {
  const phase = job.scan_phase || 'idle';
  if (job.status === 'cooling') return phaseNames.cooling;
  if (job.completion_kind === 'exception_queued' || (job.pause_reason || '').includes('复核')) return phaseNames.review;
  return phaseNames[phase] || statusNames[job.status] || job.status;
}
function resumeLabel(job) {
  if (job.completion_kind === 'exception_queued') return '继续扫描';
  if (job.status === 'paused' || job.status === 'failed') return '重新检查并恢复';
  return '继续下一批';
}

// ---------- 21 项导航元数据 ----------
const NAV_GROUPS = [
  {key: 'main', label: '主页'},
  {key: 'findings', label: '问题与线索'},
  {key: 'excluded', label: '已排除'},
  {key: 'quality', label: '数据质量'},
  {key: 'documents', label: '文档与解读'},
  {key: 'closure', label: '整改销号'},
  {key: 'governance', label: '治理'},
  {key: 'system', label: '系统'},
];
const ICON_EMOJI = {
  home: '🏠', job: '📋', issue: '⚠️', review: '🔍', data: '📊', alert: '❗',
  auto: '🤖', human: '👤', archive: '🗄️', duplicate: '⧉', recon: '🔄',
  pdf: '📄', interp: '📖', read: '📑', rect: '🔧', signoff: '✅',
  rule: '📐', kb: '📚', user: '👥', audit: '📝', settings: '⚙️',
};

// ---------- 全局状态 ----------
let sites = [];
let jobs = [];
let schedules = null;
let view = 'overview';
let currentSiteKey = null;
let overrideJobId = null;      // 从历史列表点开的任务
let reviewFilter = 'pending';
let reviewPage = 1;
let actionBusy = false;
let batchMode = false;
const batchSelected = new Set();
let firstLoadDone = false;
let navItems = [];
let currentNav = null;          // 当前选中的导航 key
let genericRowsCache = [];      // 通用视图当前数据，用于行详情展开

const siteByKey = () => Object.fromEntries(sites.map(site => [site.key, site]));
const signature = labels => (labels || []).slice().sort().join('');
function jobLabels(job) { try { return JSON.parse(job.districts_json); } catch { return []; } }
function jobsForSite(site) {
  const sig = signature(site.target_labels);
  return jobs.filter(job => signature(jobLabels(job)) === sig);
}

// ---------- 站点状态推导 ----------
function siteState(site) {
  const job = site.latest_job;
  if (site.next_action === 'existing' && job) {
    const running = `${fmtNum(job.examined_count)}/${job.estimated_total ? fmtNum(job.estimated_total) : '?'}`;
    if (job.status === 'cooling') return {code:'active', dot:'s-run', pill:['pill-run','❄ 安全冷却中'], sub:`#${job.id} 冷却中 · ${running}`};
    return {code:'active', dot:'s-run', pill:['pill-run','▶ 扫描中'], sub:`#${job.id} ${statusNames[job.status] || job.status} · ${running}`};
  }
  if (site.next_action === 'resume' && job) {
    return {code:'attention', dot:'s-warn', pill:['pill-warn','⚠ 需要处理'], sub:`⚠ #${job.id} ${statusNames[job.status] || job.status} · 可恢复`};
  }
  if (site.next_mode === 'incremental') {
    return {code:'ready', dot:'s-ok', pill:['pill-ok','✓ 已建基准'], sub:`✓ 基线 #${site.baseline_job_id} · 增量就绪`};
  }
  if (job && job.mode === 'full' && job.status === 'completed') {
    return {code:'invalid', dot:'s-warn', pill:['pill-warn','⚠ 记录不完整'], sub:'⚠ 记录不完整 · 需重新全量'};
  }
  return {code:'fresh', dot:'s-none', pill:['pill-neutral','○ 尚未扫描'], sub:'未建基准 · 待首次全量'};
}

// ---------- 侧栏（21 项导航） ----------
function renderNav(items) {
  navItems = Array.isArray(items) ? items : [];
  const container = $('#nav-items');
  if (!container) return;
  if (!navItems.length) {
    container.innerHTML = '<p class="nav-empty muted-note">未登录或当前角色暂无可见导航项</p>';
    return;
  }
  const grouped = {};
  for (const item of navItems) {
    (grouped[item.group] = grouped[item.group] || []).push(item);
  }
  let html = '';
  for (const g of NAV_GROUPS) {
    const list = grouped[g.key];
    if (!list || !list.length) continue;
    const isOpen = g.key === 'main' || list.some(item => item.key === currentNav);
    html += `<details class="side-group" data-group="${escapeHtml(g.key)}"${isOpen ? ' open' : ''}>` +
      `<summary>${escapeHtml(g.label)} <span class="g-count">${list.length}</span></summary>`;
    for (const item of list) {
      const icon = ICON_EMOJI[item.icon] || '•';
      html += `<button type="button" class="side-item" data-nav="${escapeHtml(item.key)}" data-api="${escapeHtml(item.api)}">` +
        `<span class="nav-icon">${icon}</span>` +
        `<span class="s-main"><strong>${escapeHtml(item.label)}</strong><small>${escapeHtml(item.description || '')}</small></span>` +
        `</button>`;
    }
    html += '</details>';
  }
  container.innerHTML = html;
  renderSidebar();
}

function renderSidebar() {
  const activeNav = (view === 'site') ? 'overview' : currentNav;
  document.querySelectorAll('#nav-items .side-item').forEach(item => {
    item.classList.toggle('active', item.dataset.nav === activeNav);
  });
  const activeItem = document.querySelector('#nav-items .side-item.active');
  const group = activeItem && activeItem.closest('details.side-group');
  if (group) group.open = true;

  // 站点扫描入口的告警徽标（在 overview 的 site-scan-section 上）
  let alerts = 0;
  for (const site of sites) {
    const state = siteState(site);
    if (state.code === 'attention' || state.code === 'invalid') alerts += 1;
  }
  const alertEl = $('#site-scan-alert');
  if (alertEl) {
    alertEl.hidden = alerts === 0;
    alertEl.textContent = `⚠ ${alerts}`;
  }
}

// ---------- 导航路由 ----------
async function navigate(key, apiPath) {
  currentNav = key;
  const item = navItems.find(i => i.key === key);
  const label = item ? item.label : key;
  const description = item ? (item.description || '') : '';
  if (key === 'overview') {
    showView('overview');
    renderOverviewDashboard().catch(error => toast(error.message));
    return;
  }
  if (key === 'scan-jobs') {
    showView('generic');
    await renderGenericView(label, '/api/jobs', description || '全部扫描任务列表与状态');
    return;
  }
  // 其他 19 项：通用表格视图
  showView('generic');
  await renderGenericView(label, apiPath, description);
}

// ---------- 首页总览仪表盘（/api/overview） ----------
async function renderOverviewDashboard() {
  const stats = $('#ov-stats');
  const recent = $('#ov-recent-jobs');
  if (!stats) return;
  stats.innerHTML = '<p class="muted-note panel-pad">加载仪表盘…</p>';
  if (recent) recent.innerHTML = '';
  let data;
  try {
    data = await api('/api/overview');
  } catch (error) {
    stats.innerHTML = `<div class="card"><div class="panel-pad"><p class="muted-note">仪表盘加载失败：${escapeHtml(error.message)}（未登录或权限不足）</p></div></div>`;
    return;
  }
  const buckets = data.finding_buckets || {};
  const cards = [
    {label: '活跃任务', value: data.jobs_active || 0, hint: `共 ${(data.jobs || []).length} 个近期任务`},
    {label: '站点总数', value: data.sites_total || 0, hint: '区级 + 市级'},
    {label: '客户正式问题', value: buckets.customer_formal || 0, hint: `确认 ${buckets.confirmed_problem || 0}`},
    {label: '待复核线索', value: buckets.suspected || 0, hint: `数据不足 ${buckets.insufficient_data || 0}`},
    {label: '已自动排除', value: buckets.auto_excluded || 0, hint: `人工排除 ${buckets.human_excluded || 0}`},
    {label: '逾期整改', value: data.rectification_overdue || 0, hint: '需关注'},
    {label: '异常归档', value: data.archive_findings || 0, hint: 'ARCH 问题'},
    {label: '重复对', value: data.duplicate_pairs || 0, hint: 'DUP'},
    {label: '对账匹配', value: data.recon_matches || 0, hint: '双渠道'},
  ];
  stats.innerHTML = `<div class="g-stat-grid">${cards.map(c =>
    `<div class="g-stat card"><span class="label">${escapeHtml(c.label)}</span><div class="value num">${fmtNum(c.value)}</div><div class="hint">${escapeHtml(c.hint)}</div></div>`
  ).join('')}</div>`;

  const recentJobs = (data.jobs || []).slice(0, 10);
  if (!recent || !recentJobs.length) {
    if (recent) recent.innerHTML = '<div class="card"><div class="panel-pad"><p class="muted-note">暂无最近任务</p></div></div>';
    return;
  }
  recent.innerHTML = `<div class="card" style="overflow:hidden">
    <div class="panel-head"><h3>最近任务</h3><span class="muted-note">${recentJobs.length} 条</span></div>
    <div class="table-wrap"><table class="mini">
      <thead><tr><th>任务</th><th>模式</th><th>状态</th><th>进度</th><th title="客户正式问题数 formal_finding_count">正式问题</th><th title="线索合计 finding_total_count / finding_count">线索</th><th>创建时间</th><th>结果</th></tr></thead>
      <tbody>${recentJobs.map(job => `<tr>
        <td class="num"><b>#${job.id}</b></td>
        <td>${job.mode === 'full' ? '全量' : '增量'}</td>
        <td><span class="pill ${job.status === 'completed' ? 'pill-ok' : 'pill-neutral'}">${escapeHtml(statusNames[job.status] || job.status)}</span></td>
        <td class="num">${fmtNum(job.examined_count)}/${job.estimated_total ? fmtNum(job.estimated_total) : '?'}</td>
        <td class="num" title="客户正式问题 formal_finding_count">${fmtNum(job.formal_finding_count != null ? job.formal_finding_count : 0)}</td>
        <td class="num" title="线索合计 finding_total_count">${fmtNum(job.finding_total_count != null ? job.finding_total_count : job.finding_count)}</td>
        <td class="num">${fmtTime(job.created_at)}</td>
        <td><a class="btn btn-sm" href="/api/jobs/${job.id}/export">Excel</a></td>
      </tr>`).join('')}</tbody>
    </table></div></div>`;
}

// ---------- 通用视图（19 项 + scan-jobs） ----------
async function renderGenericView(label, apiPath, description) {
  $('#generic-title').textContent = label;
  $('#generic-sub').textContent = description || '加载中…';
  $('#generic-body').innerHTML = '<div class="card"><div class="panel-pad"><p class="muted-note">加载中…</p></div></div>';
  const refreshBtn = $('#generic-refresh');
  if (refreshBtn) {
    refreshBtn.dataset.api = apiPath || '';
    refreshBtn.dataset.label = label || '';
    refreshBtn.dataset.description = description || '';
  }
  try {
    const data = await api(apiPath);
    renderGenericData(label, data);
  } catch (error) {
    $('#generic-sub').textContent = description || '';
    $('#generic-body').innerHTML = `<div class="card"><div class="panel-pad"><p class="muted-note">加载失败：${escapeHtml(error.message)}</p></div></div>`;
  }
}

function formatCellValue(value) {
  if (value == null) return '<span class="muted-note">-</span>';
  if (typeof value === 'boolean') return value ? '是' : '否';
  if (typeof value === 'number') return `<span class="num">${fmtNum(value)}</span>`;
  if (typeof value === 'string') {
    if (/^https?:\/\//i.test(value)) {
      const display = value.length > 60 ? value.slice(0, 57) + '…' : value;
      return `<a href="${safeUrl(value)}" target="_blank" rel="noopener noreferrer">${escapeHtml(display)}</a>`;
    }
    if (/\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/.test(value)) return escapeHtml(fmtTime(value));
    return escapeHtml(value.length > 200 ? value.slice(0, 197) + '…' : value);
  }
  return escapeHtml(JSON.stringify(value));
}

function renderGenericData(label, data) {
  const body = $('#generic-body');
  genericRowsCache = [];
  window.__genericLastData = data;  // 供嵌套表格行详情读取
  if (Array.isArray(data)) {
    renderGenericTable(label, data, body);
    return;
  }
  if (data && typeof data === 'object') {
    const entries = Object.entries(data);
    const isCounts = entries.length && entries.every(([_, v]) => typeof v === 'number' || v == null || typeof v === 'boolean');
    if (isCounts) {
      body.innerHTML = `<div class="g-stat-grid">${entries.map(([k, v]) =>
        `<div class="g-stat card"><span class="label">${escapeHtml(k)}</span><div class="value num">${fmtNum(Number(v) || 0)}</div></div>`
      ).join('')}</div>`;
      return;
    }
    // 混合对象：数组子项渲染为子表，标量为 kv
    let html = '';
    for (const [k, v] of entries) {
      if (Array.isArray(v) && v.length && typeof v[0] === 'object') {
        html += renderGenericTableCard(k, v);
      } else if (Array.isArray(v)) {
        html += `<div class="card"><div class="panel-head"><h3>${escapeHtml(k)}</h3><span class="muted-note">${v.length} 条</span></div><div class="panel-pad"><ul class="generic-list">${v.map(item => `<li>${escapeHtml(String(item))}</li>`).join('')}</ul></div></div>`;
      } else if (typeof v === 'object' && v !== null) {
        html += `<div class="card"><div class="panel-head"><h3>${escapeHtml(k)}</h3></div><div class="panel-pad"><dl class="kv">${Object.entries(v).map(([sk, sv]) => `<dt>${escapeHtml(sk)}</dt><dd>${escapeHtml(String(sv))}</dd>`).join('')}</dl></div></div>`;
      } else {
        html += `<div class="card"><div class="panel-pad"><dl class="kv"><dt>${escapeHtml(k)}</dt><dd>${escapeHtml(String(v))}</dd></dl></div></div>`;
      }
    }
    body.innerHTML = html || '<div class="card"><div class="panel-pad"><p class="muted-note">暂无数据</p></div></div>';
    bindGenericRowButtons(body);
    return;
  }
  body.innerHTML = `<div class="card"><div class="panel-pad"><p class="muted-note">${escapeHtml(String(data))}</p></div></div>`;
}

function renderGenericTable(label, rows, container) {
  if (!rows.length) {
    container.innerHTML = `<div class="card"><div class="panel-pad"><p class="muted-note">暂无数据</p></div></div>`;
    return;
  }
  const keys = Object.keys(rows[0]).filter(k => {
    const v = rows[0][k];
    return v == null || typeof v !== 'object';
  });
  container.innerHTML = `<div class="card" style="overflow:hidden">
    <div class="panel-head"><h3>${escapeHtml(label)} <span class="muted-note">共 ${rows.length} 条</span></h3></div>
    <div class="table-wrap"><table class="mini generic-table">
      <thead><tr>${keys.map(k => `<th>${escapeHtml(k)}</th>`).join('')}<th>操作</th></tr></thead>
      <tbody>${rows.map((row, idx) => `<tr>${keys.map(k => `<td>${formatCellValue(row[k])}</td>`).join('')}<td><button type="button" class="btn btn-sm generic-row-view" data-idx="${idx}">查看</button></td></tr>`).join('')}</tbody>
    </table></div></div>`;
  genericRowsCache = rows;
  bindGenericRowButtons(container);
}

function renderGenericTableCard(label, rows) {
  if (!rows.length) return '';
  const keys = Object.keys(rows[0]).filter(k => {
    const v = rows[0][k];
    return v == null || typeof v !== 'object';
  });
  return `<div class="card" style="overflow:hidden">
    <div class="panel-head"><h3>${escapeHtml(label)}</h3><span class="muted-note">${rows.length} 条</span></div>
    <div class="table-wrap"><table class="mini generic-table">
      <thead><tr>${keys.map(k => `<th>${escapeHtml(k)}</th>`).join('')}<th>操作</th></tr></thead>
      <tbody>${rows.map((row, idx) => `<tr>${keys.map(k => `<td>${formatCellValue(row[k])}</td>`).join('')}<td><button type="button" class="btn btn-sm generic-row-view" data-key="${escapeHtml(label)}" data-idx="${idx}">查看</button></td></tr>`).join('')}</tbody>
    </table></div></div>`;
}

function bindGenericRowButtons(scope) {
  scope.querySelectorAll('.generic-row-view').forEach(btn => {
    btn.addEventListener('click', () => openGenericRow(btn));
  });
}

function openGenericRow(btn) {
  const idx = Number(btn.dataset.idx);
  const key = btn.dataset.key || '';
  let row = null;
  if (key) {
    // 嵌套表格：从 generic-body 当前 DOM 重新读取（renderGenericData 写入）
    const data = window.__genericLastData;
    row = data && Array.isArray(data[key]) ? data[key][idx] : null;
  } else if (genericRowsCache.length) {
    row = genericRowsCache[idx];
  }
  if (!row) return;
  // 形似 finding（id + rule_code）：优先打开证据定位对话框
  if (row.id != null && row.rule_code && (row.district || row.title || row.url)) {
    openEvidence(Number(row.id)).catch(error => toast(error.message));
    return;
  }
  // 否则在表格下方展开详情抽屉
  showGenericDetail(row);
}

function showGenericDetail(row) {
  let panel = $('#generic-detail');
  if (!panel) {
    panel = document.createElement('div');
    panel.id = 'generic-detail';
    panel.className = 'card generic-detail';
    $('#generic-body').appendChild(panel);
  }
  panel.innerHTML = `<div class="panel-head"><h3>详情</h3><button type="button" class="btn btn-sm" id="generic-detail-close">关闭</button></div>
    <div class="panel-pad"><dl class="kv">${Object.entries(row).map(([k, v]) =>
      `<dt>${escapeHtml(k)}</dt><dd>${v == null ? '<span class="muted-note">-</span>' : escapeHtml(typeof v === 'object' ? JSON.stringify(v) : String(v))}</dd>`
    ).join('')}</dl></div>`;
  panel.scrollIntoView({behavior: 'smooth', block: 'nearest'});
  $('#generic-detail-close').addEventListener('click', () => panel.remove());
}

// ---------- 总览（站点卡片 + 批量） ----------
function renderOverview() {
  const counts = {attention: 0, active: 0, ready: 0, fresh: 0};
  const cards = [];
  for (const site of sites) {
    const state = siteState(site);
    if (state.code === 'invalid' || state.code === 'attention') counts.attention += 1;
    else counts[state.code] += 1;
    const act = {
      active: '查看进度 →', attention: '处理并恢复 →', ready: '查看详情 →',
      invalid: '重新建立基准 →', fresh: '开始首次全量 →',
    }[state.code];
    const check = batchMode ? `<span class="batch-check ${batchSelected.has(site.key) ? 'on' : ''}">${batchSelected.has(site.key) ? '✓' : ''}</span>` : '';
    cards.push(`<button type="button" class="ov-card ${batchSelected.has(site.key) && batchMode ? 'selected' : ''}" data-key="${site.key}">
      ${check}
      <span class="r1"><strong>${escapeHtml(site.district)} <span class="lv ${site.source_level === '区级' ? 'lv-district' : 'lv-city'}">${escapeHtml(site.source_level)}</span></strong><span class="pill ${state.pill[0]}">${state.pill[1]}</span></span>
      <span class="r2">${escapeHtml(state.sub)}</span>
      <span class="act">${act}</span>
    </button>`);
  }
  $('#ov-chips').innerHTML = `
    <span class="ov-chip">⚠ 需要处理 <b class="num">${counts.attention}</b></span>
    <span class="ov-chip">▶ 进行中 <b class="num">${counts.active}</b></span>
    <span class="ov-chip">✓ 已建基准 <b class="num">${counts.ready}</b></span>
    <span class="ov-chip">○ 未建基准 <b class="num">${counts.fresh}</b></span>`;
  $('#ov-grid').innerHTML = cards.join('');
  renderLegacyJobs();
  syncBatchBar();
}

function renderLegacyJobs() {
  const knownSignatures = new Set(sites.map(site => signature(site.target_labels)));
  const legacy = jobs.filter(job => !knownSignatures.has(signature(jobLabels(job))));
  $('#legacy-card').hidden = legacy.length === 0;
  if (!legacy.length) return;
  $('#legacy-body').innerHTML = legacy.map(job => `
    <tr>
      <td class="num"><b>#${job.id}</b></td>
      <td>${escapeHtml(jobLabels(job).join('、'))}</td>
      <td>${job.mode === 'full' ? '全量' : '增量'}</td>
      <td><span class="pill ${job.status === 'completed' ? 'pill-ok' : 'pill-neutral'}">${statusNames[job.status] || job.status}</span></td>
      <td class="num">${fmtNum(job.examined_count)}/${job.estimated_total ? fmtNum(job.estimated_total) : '?'}</td>
      <td class="num" title="客户正式问题 formal_finding_count">${fmtNum(job.formal_finding_count != null ? job.formal_finding_count : 0)}</td><td class="num" title="线索合计 finding_total_count">${fmtNum(job.finding_total_count != null ? job.finding_total_count : job.finding_count)}</td>
      <td class="num">${fmtTime(job.created_at)}</td>
      <td><a class="btn btn-sm" href="/api/jobs/${job.id}/export">Excel</a></td>
    </tr>`).join('');
}

// ---------- 批量扫描 ----------
function syncBatchBar() {
  $('#batch-bar').hidden = !batchMode;
  $('#batch-toggle').textContent = batchMode ? '退出批量' : '批量扫描';
  if (!batchMode) return;
  const n = batchSelected.size;
  const lookup = siteByKey();
  const withBase = [...batchSelected].filter(key => lookup[key] && lookup[key].next_mode === 'incremental').length;
  $('#batch-summary').innerHTML = n
    ? `已选 <strong>${n}</strong> 个站点 · 将执行：增量 ×${withBase}、全量 ×${n - withBase}`
    : '未选择站点';
  $('#batch-start').disabled = actionBusy || n === 0;
}

// ---------- 站点详情 ----------
function displayJobFor(site) {
  if (overrideJobId) {
    if (site.latest_job && site.latest_job.id === overrideJobId) return site.latest_job;
    const found = jobs.find(job => job.id === overrideJobId);
    if (found) return found;
  }
  return site.latest_job || null;
}

function heroConfig(site, state, job) {
  if (state.code === 'active') {
    const cooling = job.status === 'cooling';
    return {
      title: cooling ? '安全冷却中，倒计时结束后自动继续' : '正在扫描中，无需操作',
      desc: cooling ? cooldownText(job.cooldown_until) : '可暂停以保留当前位置，或停止本任务。关闭页面不影响扫描。',
      buttons: [
        {label: '暂停', action: 'pause', hidden: !['running','pending'].includes(job.status)},
        {label: '停止任务', action: 'stop', danger: true},
      ],
      more: [{label: '下载当前结果 Excel', href: `/api/jobs/${job.id}/export`}],
    };
  }
  if (state.code === 'attention') {
    return {
      title: `任务 #${job.id} 暂停等待处理`,
      desc: '确认下方原因后点击恢复，系统会重新检查并从断点继续，进度不丢失。',
      buttons: [
        {label: resumeLabel(job), action: 'resume', primary: true},
        {label: '停止任务', action: 'stop', danger: true},
      ],
      more: [
        {label: '下载当前结果 Excel', href: `/api/jobs/${job.id}/export`},
        {divider: '以下操作有代价，需二次确认'},
        {label: '重新全量扫描…', action: 'rebuild', danger: true},
      ],
    };
  }
  if (state.code === 'ready') {
    return {
      title: '一切正常，无需操作',
      desc: `已建立基准数据库（基线 #${site.baseline_job_id}）。如需立即检查，可手动执行一次增量扫描，仅核验新增与变化条目。`,
      buttons: [{label: '立即增量扫描', action: 'start'}],
      more: [
        ...(job ? [{label: '下载最近结果 Excel', href: `/api/jobs/${job.id}/export`}] : []),
        {divider: '以下操作有代价，需二次确认'},
        {label: '重新全量扫描…', action: 'rebuild', danger: true},
      ],
    };
  }
  if (state.code === 'invalid') {
    return {
      title: '需要重新建立基准数据库',
      desc: `任务 #${job.id} 的逐条记录不完整，不能作为增量基准。重新执行一次完整全量即可修复。`,
      buttons: [{label: '重新建立基准（全量）', action: 'start', primary: true}],
      more: [
        {label: '下载已有结果 Excel', href: `/api/jobs/${job.id}/export`},
        {divider: '等价操作，同样需要完整扫描'},
        {label: '重新全量扫描…', action: 'rebuild', danger: true},
      ],
      showAdvanced: true,
    };
  }
  return {
    title: '第一步：建立基准数据库',
    desc: '首次需要完整扫描一遍（可随时暂停、进度不丢失）。完成后系统自动转入增量模式，之后每次只需几分钟。',
    buttons: [{label: '开始首次全量扫描', action: 'start', primary: true}],
    more: [],
    showAdvanced: true,
    showSteps: true,
  };
}

function renderHero(site, state, job) {
  const config = heroConfig(site, state, job);
  $('#hero-title').textContent = config.title;
  $('#hero-desc').textContent = config.desc;
  $('#hero-buttons').innerHTML = config.buttons.filter(button => !button.hidden).map(button =>
    `<button type="button" class="btn ${button.primary ? 'btn-primary btn-lg' : ''} ${button.danger ? 'btn-danger' : ''}" data-action="${button.action}" ${actionBusy ? 'disabled' : ''}>${escapeHtml(button.label)}</button>`
  ).join('');
  const menu = $('#more-menu');
  const items = config.more || [];
  menu.hidden = items.length === 0;
  $('#more-menu-items').innerHTML = items.map(item => {
    if (item.divider) return `<div class="menu-note">${escapeHtml(item.divider)}</div>`;
    if (item.href) return `<a class="btn btn-sm" style="border:0;justify-content:flex-start;width:100%" href="${safeUrl(item.href)}">${escapeHtml(item.label)}</a>`;
    return `<button type="button" class="${item.danger ? 'danger' : ''}" data-action="${item.action}">${escapeHtml(item.label)}</button>`;
  }).join('');
  $('#max-docs-adv').hidden = !config.showAdvanced;
  $('#onboarding-steps').hidden = !config.showSteps;
}

function renderCallout(job) {
  const callout = $('#site-callout');
  if (!job) { callout.hidden = true; return; }
  const message = job.pause_reason || job.last_error || '';
  const resumable = ['paused','partial','failed'].includes(job.status);
  if (!message || !resumable) { callout.hidden = true; return; }
  callout.hidden = false;
  let title = '为什么暂停了？';
  let desc = message;
  if (job.status === 'partial') title = '本批次已完成设定上限';
  if (job.status === 'failed') title = '扫描异常结束';
  if (message.includes('覆盖校验失败')) {
    title = '还有个别条目未完成核验';
    desc = `收尾校验发现少量条目未达终态。点击「${resumeLabel(job)}」，系统会自动补扫这些遗留条目并重新校验。原始信息：${message}`;
  }
  $('#callout-title').textContent = title;
  $('#callout-desc').textContent = desc;
}

function renderProgress(job) {
  const wrap = $('#progress-wrap');
  if (!job) { wrap.hidden = true; return; }
  wrap.hidden = false;
  const examined = Number(job.examined_count || 0);
  const total = Number(job.estimated_total || 0);
  const percent = total ? Math.min(100, examined / total * 100) : 0;
  const percentText = total ? `${percent.toFixed(percent >= 10 ? 1 : 2)}%` : '估算中';
  $('#progress-big').innerHTML = `${percentText}<small>${fmtNum(examined)} / ${total ? fmtNum(total) : '?'} 条</small>`;
  const parts = [`${job.mode === 'full' ? '全量扫描' : `增量扫描 · 基线 #${job.baseline_job_id ?? '-'}`}`, `阶段 ${phaseLabel(job)}`];
  if (job.current_district) parts.push(`${job.current_district} 第 ${job.current_page || '-'} 页`);
  $('#progress-eta').textContent = parts.join(' · ');
  $('#progress-bar').style.width = `${percent}%`;
}

const itemStatsCache = {};
function renderStats(site, job) {
  const row = $('#stat-row');
  if (!job) { row.hidden = true; return; }
  row.hidden = false;
  let gen = {};
  try { gen = JSON.parse(job.generation_stats_json || '{}'); } catch { gen = {}; }
  const discovered = gen.discovered ?? job.estimated_total;
  const added = gen.new ?? 0;
  const reused = gen.reused ?? job.skipped_count;
  const cached = itemStatsCache[job.id];
  row.innerHTML = `
    <div class="stat"><span class="label">发现条目</span><div class="value num">${discovered != null ? fmtNum(discovered) : '?'}</div></div>
    <div class="stat"><span class="label">新增</span><div class="value num">${fmtNum(added)}</div></div>
    <div class="stat"><span class="label">基线复用</span><div class="value num">${fmtNum(reused)}</div><div class="hint">未开详情直接复用</div></div>
    <div class="stat"><span class="label">表头完整</span><div class="value num" id="stat-header">${cached ? fmtNum(cached.complete_header) : '…'}</div><div class="hint" id="stat-header-hint">${cached ? `缺字段 ${fmtNum(cached.incomplete_header)} · 无表头 PASS ${fmtNum(cached.no_header_pass)}` : '读取中'}</div></div>
    <div class="stat"><span class="label">客户正式问题</span><div class="value num link" id="stat-findings">${fmtNum(job.formal_finding_count != null ? job.formal_finding_count : 0)}</div><div class="hint">线索合计 ${fmtNum(job.finding_total_count != null ? job.finding_total_count : job.finding_count)} · 点击复核 ↓</div></div>`;
  const findingsTile = $('#stat-findings');
  if (findingsTile) findingsTile.addEventListener('click', () => {
    const panel = $('#findings-panel');
    if (!panel.hidden) panel.scrollIntoView({behavior: 'smooth'});
  });
  api(`/api/jobs/${job.id}/item-stats`).then(stats => {
    itemStatsCache[job.id] = stats;
    const header = $('#stat-header'), hint = $('#stat-header-hint');
    if (!header) return;
    header.textContent = fmtNum(stats.complete_header);
    hint.textContent = `缺字段 ${fmtNum(stats.incomplete_header)} · 无表头 PASS ${fmtNum(stats.no_header_pass)}`;
  }).catch(() => {});
}

function renderTech(job) {
  const tech = $('#tech-details');
  if (!job) { tech.hidden = true; return; }
  tech.hidden = false;
  $('#tech-url').textContent = job.current_url || '-';
  $('#tech-message').textContent = job.pause_reason || job.last_error || '-';
  $('#tech-cooldown').textContent = job.status === 'cooling' ? cooldownText(job.cooldown_until) : '无需冷却';
  api(`/api/jobs/${job.id}/generation`).then(data => {
    const c = data.counts || {};
    const g = data.generation || {};
    $('#tech-generation').textContent = `快照阶段 ${phaseNames[data.scan_phase] || data.scan_phase} · 发现 ${c.total || 0} · 完成 ${(c.completed||0)+(c.reused||0)} · pending ${c.pending||0} · retry ${c.retry||0} · review ${c.review||0} · 观测总量 ${g.observed_total ?? '-'}`;
  }).catch(() => { $('#tech-generation').textContent = '快照信息暂不可用'; });
}

function renderRail(site, state, job) {
  const info = [
    `<dt>来源</dt><dd>${escapeHtml(site.source_name)}（${escapeHtml(site.host)}）</dd>`,
    `<dt>层级</dt><dd>${escapeHtml(site.source_level)} · ${site.target_count > 1 ? `${site.target_count} 个栏目` : '单一来源'}</dd>`,
  ];
  if (site.target_count > 1) {
    info.push(`<dt>覆盖栏目</dt><dd class="chips">${site.target_labels.map(label => `<span>${escapeHtml(label.split('·').pop())}</span>`).join('')}</dd>`);
  }
  if (site.baseline_job_id) info.push(`<dt>基准</dt><dd>基线 #${site.baseline_job_id}</dd>`);
  info.push(`<dt>并行</dt><dd>${site.source_level === '市级' ? '与其余市级区县同域排队' : '与市级平台不同域，可并行'}</dd>`);
  $('#rail-info').innerHTML = info.join('');

  const body = $('#rail-schedule-body');
  if (!schedules) { body.textContent = '读取中…'; }
  else {
    const entry = (schedules.sites || []).find(item => item.site_key === site.key);
    if (!schedules.enabled) {
      body.innerHTML = '<p class="muted-note">持续调度未开启（服务端未设置 CONTINUOUS_SCAN_ENABLED），不会自动执行任务。</p>';
    } else if (!entry || !entry.has_baseline) {
      body.innerHTML = '<p class="muted-note">建立基准数据库后自动开启：每 6 小时增量、每 7 天全量对账，无需手动配置。</p>';
    } else {
      body.innerHTML = `
        <div class="next-run"><span class="when num">${fmtTime(entry.next_incremental_at)}</span><span class="pill pill-run">▶ 增量</span></div>
        <div class="next-run"><span class="when num">${fmtTime(entry.next_full_reconcile_at)}</span><span class="pill pill-neutral">◎ 对账</span></div>
        <p class="muted-note" style="margin-top:8px">上次增量 ${fmtTime(entry.last_incremental_at)} · 上次对账 ${fmtTime(entry.last_full_reconcile_at)}</p>`;
    }
  }
  $('#rail-notes-card').hidden = !(state.code === 'fresh' || state.code === 'invalid');
  $('#rail-findings-card').hidden = !job || !Number(job.formal_finding_count != null ? job.formal_finding_count : 0);
  if (job) $('#rail-findings-num').textContent = fmtNum(job.formal_finding_count != null ? job.formal_finding_count : 0);
}

function renderSiteHistory(site) {
  const rows = jobsForSite(site);
  $('#site-history-empty').hidden = rows.length > 0;
  $('#site-history-body').innerHTML = rows.map(job => {
    const examined = Number(job.examined_count || 0);
    const total = Number(job.estimated_total || 0);
    const percent = total ? Math.min(100, examined / total * 100) : 0;
    const recorded = Number(job.scan_item_count || 0);
    const incomplete = job.status === 'completed' && recorded !== examined;
    const pillClass = ['paused','partial','failed'].includes(job.status) ? 'pill-warn'
      : ['running','pending','cooling'].includes(job.status) ? 'pill-run'
      : job.status === 'completed' ? 'pill-ok' : 'pill-neutral';
    const isBaseline = site.baseline_job_id === job.id;
    return `<tr>
      <td><button type="button" class="job-open num" data-id="${job.id}">#${job.id}</button></td>
      <td>${job.mode === 'full' ? '全量' : `增量<small> · 基线 #${job.baseline_job_id ?? '-'}</small>`}</td>
      <td><span class="pill ${pillClass}">${statusNames[job.status] || job.status}${isBaseline ? ' · 现行基准' : ''}</span>${incomplete ? ' <em class="history-warning">记录不完整</em>' : ''}</td>
      <td><span class="mini-progress"><span style="width:${percent}%"></span></span><span class="num">${fmtNum(examined)}/${total ? fmtNum(total) : '?'}</span></td>
      <td class="num" title="客户正式问题 formal_finding_count">${fmtNum(job.formal_finding_count != null ? job.formal_finding_count : 0)}</td><td class="num" title="线索合计 finding_total_count">${fmtNum(job.finding_total_count != null ? job.finding_total_count : job.finding_count)}</td>
      <td class="num">${fmtTime(job.created_at)}</td>
      <td><a class="btn btn-sm" href="/api/jobs/${job.id}/export">Excel</a></td>
    </tr>`;
  }).join('');
}

let renderedFindingsJob = null;
function renderSitePage() {
  const site = siteByKey()[currentSiteKey];
  if (!site) { showView('overview'); return; }
  const state = siteState(site);
  const job = displayJobFor(site);
  $('#site-title').textContent = site.district;
  const level = $('#site-level');
  level.textContent = site.source_level === '区级' ? '区级网站' : '市级平台';
  level.className = `lv ${site.source_level === '区级' ? 'lv-district' : 'lv-city'}`;
  const pill = $('#site-pill');
  pill.className = `pill ${state.pill[0]}`;
  pill.textContent = state.pill[1];
  const subParts = [`${site.source_name} · ${site.host}`];
  if (overrideJobId && job) subParts.push(`正在查看历史任务 #${job.id}（点击侧栏站点名可回到最新状态）`);
  $('#site-subline').textContent = subParts.join(' · ');
  renderHero(site, state, job);
  renderCallout(job);
  renderProgress(job);
  renderStats(site, job);
  renderTech(job);
  renderRail(site, state, job);
  renderSiteHistory(site);
  if (job) {
    const focusInside = $('#findings-panel').contains(document.activeElement);
    if (!focusInside) renderFindings(job.id).catch(() => {});
    renderScanExceptions(job.id).catch(() => {});
  } else {
    $('#findings-panel').hidden = true;
    $('#exceptions-panel').hidden = true;
  }
}

// ---------- 问题复核 ----------
async function renderFindings(jobId) {
  const queue = await api(`/api/jobs/${jobId}/review-queue?review_status=${reviewFilter}&page=${reviewPage}&page_size=10`);
  if (renderedFindingsJob !== jobId) { renderedFindingsJob = jobId; }
  const panel = $('#findings-panel');
  const totalCount = Object.values(queue.counts).reduce((sum, value) => sum + value, 0);
  panel.hidden = totalCount === 0;
  if (panel.hidden) return;
  const buckets = queue.bucket_counts || {};
  const setText = (id, val) => { const el = $(id); if (el) el.textContent = fmtNum(val || 0); };
  setText('#rc-pending', queue.counts.pending);
  setText('#rc-suspected', buckets.suspected);
  setText('#rc-confirmed-problem', buckets.confirmed_problem);
  setText('#rc-insufficient', buckets.insufficient_data);
  setText('#rc-collection', buckets.collection_error);
  setText('#rc-auto', buckets.auto_excluded);
  setText('#rc-human', buckets.human_excluded);
  setText('#rc-customer-formal', buckets.customer_formal);
  $('#rc-pending').textContent = fmtNum(queue.counts.pending);
  $('#rc-confirmed').textContent = fmtNum(queue.counts.confirmed);
  $('#rc-dismissed').textContent = fmtNum(queue.counts.dismissed);
  document.querySelectorAll('.review-filter').forEach(button => {
    button.classList.toggle('active', button.dataset.status === reviewFilter);
  });
  const totalPages = Math.max(1, Math.ceil(queue.total / queue.page_size));
  $('#review-page-info').textContent = queue.total ? `第 ${queue.page} / ${totalPages} 页，共 ${fmtNum(queue.total)} 条` : '当前筛选下没有问题';
  $('#review-prev').disabled = queue.page <= 1;
  $('#review-next').disabled = queue.page >= totalPages;
  const body = $('#findings-body'); body.innerHTML = '';
  for (const finding of queue.items) {
    const card = document.createElement('article');
    card.className = 'finding-card';
    card.innerHTML = `<div class="finding-card-header"><span class="finding-district">${escapeHtml(finding.district)}</span><a href="${safeUrl(finding.url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(finding.title)}</a></div>
      <div class="finding-content"><div><span class="finding-label">问题</span><strong>${escapeHtml(finding.category)}</strong><p>${escapeHtml(finding.detail)}</p></div><div><span class="finding-label">证据</span><p>${escapeHtml(finding.evidence || '-')}</p></div></div>
      <div class="finding-controls"><label>复核结论<select class="review-decision" data-id="${finding.id}"><option value="pending">待复核</option><option value="confirmed">确认异常</option><option value="dismissed">排除</option></select></label><label class="review-note-label">备注<input class="review-note" data-id="${finding.id}" value="${escapeHtml(finding.review_note)}" maxlength="500"></label><div class="finding-actions"><button type="button" class="evidence-open" data-id="${finding.id}">定位</button><button type="button" class="review-save" data-id="${finding.id}">保存</button></div></div>`;
    body.appendChild(card);
    card.querySelector('.review-decision').value = finding.review_status;
  }
  document.querySelectorAll('.review-save').forEach(button => button.addEventListener('click', async () => {
    const id = button.dataset.id;
    const decision = document.querySelector(`.review-decision[data-id="${id}"]`).value;
    const note = document.querySelector(`.review-note[data-id="${id}"]`).value;
    try {
      await api(`/api/findings/${id}/review`, {method:'POST', body:JSON.stringify({decision, note})});
      toast('复核结论已保存');
      renderFindings(jobId).catch(() => {});
    } catch (error) { toast(error.message); }
  }));
  document.querySelectorAll('.evidence-open').forEach(button => button.addEventListener('click', async () => {
    try { await openEvidence(Number(button.dataset.id)); } catch (error) { toast(error.message); }
  }));
}

async function renderScanExceptions(jobId) {
  const queue = await api(`/api/jobs/${jobId}/scan-exceptions`);
  const panel = $('#exceptions-panel');
  panel.hidden = queue.counts.review_required === 0;
  if (panel.hidden) return;
  $('#exception-counts').innerHTML = `<span class="ov-chip">待复测 <b>${queue.counts.pending}</b></span><span class="ov-chip">复测已恢复 <b>${queue.counts.resolved}</b></span><span class="ov-chip">待人工复核 <b>${queue.counts.review_required}</b></span>`;
  const body = $('#scan-exceptions-body'); body.innerHTML = '';
  for (const exception of queue.items) {
    const card = document.createElement('article');
    card.className = 'finding-card';
    card.innerHTML = `<div class="finding-card-header"><span class="finding-district">${escapeHtml(exception.district)}</span><a href="${safeUrl(exception.url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(exception.title)}</a></div>
      <div class="finding-content"><div><span class="finding-label">扫描位置</span><strong>第 ${exception.page_number} 页，第 ${exception.item_index + 1} 条</strong><p>${escapeHtml(exception.category)}</p></div><div><span class="finding-label">复测结果</span><p>${escapeHtml(exception.last_error)}</p><p>首次记录：${escapeHtml(fmtTime(exception.first_seen_at))}</p></div></div>`;
    body.appendChild(card);
  }
}

async function openEvidence(findingId) {
  const finding = await api(`/api/findings/${findingId}/evidence`);
  $('#evidence-subtitle').textContent = `${finding.district} · ${finding.title} · ${finding.rule_code}`;
  const source = $('#evidence-source');
  if (source) source.href = validatedUrl(finding.url);
  const fieldsBox = $('#evidence-fields');
  const fields = finding.evidence_fields || [];
  if (fieldsBox) {
    if (fields.length) {
      fieldsBox.innerHTML = fields.map(item => `<div class="evidence-field"><span class="finding-label">${escapeHtml(item.field_name || item.source_type || '字段')}</span><p>${escapeHtml(item.raw_value || item.excerpt || '-')}</p></div>`).join('');
    } else {
      fieldsBox.innerHTML = `<div class="evidence-field"><span class="finding-label">机器状态</span><p>${escapeHtml(finding.machine_status || '-')}</p></div>
        <div class="evidence-field"><span class="finding-label">人工状态</span><p>${escapeHtml(finding.human_status || '-')}</p></div>
        <div class="evidence-field"><span class="finding-label">规则版本</span><p>${escapeHtml(finding.rule_version || '-')}</p></div>
        <div class="evidence-field"><span class="finding-label">详情</span><p>${escapeHtml(finding.detail || '-')}</p></div>`;
    }
  }
  const pageVal = $('#evidence-page-value');
  const bodyVal = $('#evidence-body-value');
  if (pageVal) pageVal.textContent = finding.page_value || '-';
  if (bodyVal) bodyVal.textContent = finding.body_value || '-';
  const ruleText = $('#evidence-rule-text');
  if (ruleText) ruleText.textContent = finding.evidence || finding.detail || '-';
  const body = $('#evidence-body');
  if (body) body.innerHTML = highlightEvidence(finding.body_text, [finding.page_value, finding.body_value, ...(fields.map(f => f.raw_value || ''))]);
  $('#evidence-dialog').showModal();
}

// ---------- 任务中心 ----------
async function openTaskCenter() {
  const dialog = $('#task-center-dialog');
  const box = $('#task-center-body');
  box.textContent = '加载中…';
  if (!dialog.open) dialog.showModal();
  try {
    const data = await api('/api/continuous-schedules');
    schedules = data;
    renderTaskCenter(data);
  } catch (_) {
    box.textContent = '任务计划读取失败，请稍后重试';
  }
}

function renderTaskCenter(data) {
  const box = $('#task-center-body');
  const rows = (data && data.sites) || [];
  const willRun = entry => entry.has_baseline && entry.opted_in && entry.enabled;
  const planned = rows.filter(willRun);
  const blocked = rows.filter(entry => !willRun(entry));
  const nextAt = entry => {
    const times = [entry.next_incremental_at, entry.next_full_reconcile_at].filter(Boolean).map(value => new Date(value).getTime());
    return times.length ? Math.min(...times) : Infinity;
  };
  planned.sort((left, right) => nextAt(left) - nextAt(right));
  const parts = [];
  if (!data || !data.enabled) {
    parts.push('<p class="task-center-note">持续调度未开启（服务端未设置 CONTINUOUS_SCAN_ENABLED），系统不会自动执行任何任务；下方仅展示各站点基准数据库状态。</p>');
  }
  const plannedRows = planned.map(entry => `
    <div class="schedule-row">
      <strong>${escapeHtml(entry.site_label)}</strong>
      <span class="schedule-times num">
        下次增量 ${escapeHtml(fmtTime(entry.next_incremental_at))} · 下次对账 ${escapeHtml(fmtTime(entry.next_full_reconcile_at))}<br>
        上次增量 ${escapeHtml(fmtTime(entry.last_incremental_at))} · 上次对账 ${escapeHtml(fmtTime(entry.last_full_reconcile_at))} · 基线 #${entry.baseline_job_id ?? '-'}
      </span>
    </div>`);
  parts.push(`<div class="dlg-group"><h4>将自动执行 <span class="count-badge">${planned.length} 个站点</span></h4>${plannedRows.join('') || '<p class="task-center-empty">暂无。站点需要先建立基准数据库（完成一次完整全量扫描）。</p>'}</div>`);
  if (blocked.length) {
    const reason = entry => !entry.has_baseline ? '未建基准' : (!entry.opted_in ? '未纳入调度' : '调度已停用');
    parts.push(`<div class="dlg-group"><h4>不会自动执行 <span class="count-badge">${blocked.length} 个站点</span></h4>
      <div class="waiting-sites">${blocked.map(entry => `<span>${escapeHtml(entry.site_label)} · ${reason(entry)}</span>`).join('')}</div>
      <p class="task-center-empty" style="margin-top:8px">完成一次完整全量扫描并通过覆盖校验后，站点会自动进入增量调度（增量每 6 小时、全量对账每 7 天）。</p></div>`);
  }
  box.innerHTML = parts.join('');
}

// ---------- 动作 ----------
function setBusy(busy) { actionBusy = busy; refreshUi(); }

async function startSites(siteKeys, maxDocuments) {
  if (!siteKeys.length) return toast('请至少选择一个扫描站点');
  setBusy(true);
  try {
    const result = await api('/api/site-jobs', {method:'POST', body:JSON.stringify({site_keys: siteKeys, max_documents: Number(maxDocuments || 0)})});
    const names = result.jobs.map(job => `${job.site_label} #${job.job_id}`).join('、');
    toast(`已启动：${names}`);
    overrideJobId = null;
    batchSelected.clear();
    batchMode = false;
    await poll();
  } catch (error) { toast(error.message); }
  finally { setBusy(false); }
}

async function rebuildSite(siteKey) {
  setBusy(true);
  try {
    const result = await api('/api/site-jobs/full-rebuild', {method:'POST', body:JSON.stringify({site_keys: [siteKey]})});
    const names = result.jobs.map(job => `${job.site_label} #${job.job_id}`).join('、');
    toast(`已创建全量重扫：${names}`);
    overrideJobId = null;
    await poll();
  } catch (error) { toast(error.message); }
  finally { setBusy(false); }
}

async function jobCommand(jobId, action) {
  setBusy(true);
  try { await api(`/api/jobs/${jobId}/${action}`, {method:'POST'}); await poll(); }
  catch (error) { toast(error.message); }
  finally { setBusy(false); }
}

function handleHeroAction(action) {
  const site = siteByKey()[currentSiteKey];
  if (!site) return;
  const job = displayJobFor(site);
  document.querySelectorAll('details.more-menu').forEach(menu => { menu.open = false; });
  if (action === 'start') {
    const maxDocs = $('#max-docs-adv').hidden ? 0 : Number($('#site-max-docs').value || 0);
    startSites([site.key], maxDocs);
  } else if (action === 'rebuild') {
    $('#confirm-rebuild-site').textContent = site.label;
    $('#confirm-rebuild').dataset.siteKey = site.key;
    $('#confirm-rebuild').showModal();
  } else if (['pause','resume','stop'].includes(action) && job) {
    jobCommand(job.id, action);
  }
}

// ---------- 视图切换与轮询 ----------
function showView(nextView, siteKey) {
  view = nextView;
  if (nextView === 'site' && siteKey) {
    if (currentSiteKey !== siteKey) { overrideJobId = null; reviewFilter = 'pending'; reviewPage = 1; renderedFindingsJob = null; }
    currentSiteKey = siteKey;
  }
  $('#page-overview').hidden = view !== 'overview';
  $('#page-site').hidden = view !== 'site';
  $('#page-generic').hidden = view !== 'generic';
  refreshUi();
  window.scrollTo({top: 0});
}

function refreshUi() {
  renderSidebar();
  if (view === 'overview') renderOverview();
  else if (view === 'site') renderSitePage();
  // generic 视图由 navigate/renderGenericView 自行渲染，不参与 poll
}

async function poll() {
  const [jobsData, sitesData, schedulesData] = await Promise.all([
    api('/api/jobs'),
    api('/api/scan-sites'),
    api('/api/continuous-schedules').catch(() => schedules),
  ]);
  jobs = jobsData;
  sites = sitesData;
  schedules = schedulesData;
  if (!firstLoadDone) {
    firstLoadDone = true;
    const states = sites.map(site => ({site, state: siteState(site)}));
    const attention = states.find(entry => ['attention','invalid'].includes(entry.state.code));
    const active = states.find(entry => entry.state.code === 'active');
    if (attention) showView('site', attention.site.key);
    else if (active) showView('site', active.site.key);
    else showView('overview');
    return;
  }
  refreshUi();
}

// ---------- 事件绑定 ----------
$('#nav-sidebar').addEventListener('click', event => {
  const item = event.target.closest('.side-item');
  if (!item || !item.dataset.nav) return;
  navigate(item.dataset.nav, item.dataset.api).catch(error => toast(error.message));
});

$('#ov-grid').addEventListener('click', event => {
  const card = event.target.closest('.ov-card');
  if (!card) return;
  if (batchMode) {
    const key = card.dataset.key;
    if (batchSelected.has(key)) batchSelected.delete(key); else batchSelected.add(key);
    renderOverview();
    return;
  }
  showView('site', card.dataset.key);
});

$('#batch-toggle').addEventListener('click', () => { batchMode = !batchMode; if (!batchMode) batchSelected.clear(); renderOverview(); });
$('#batch-cancel').addEventListener('click', () => { batchMode = false; batchSelected.clear(); renderOverview(); });
$('#batch-start').addEventListener('click', () => startSites([...batchSelected], Number($('#batch-max-docs').value || 0)));

$('#hero-buttons').addEventListener('click', event => {
  const button = event.target.closest('[data-action]');
  if (button) handleHeroAction(button.dataset.action);
});
$('#more-menu-items').addEventListener('click', event => {
  const button = event.target.closest('button[data-action]');
  if (button) handleHeroAction(button.dataset.action);
});
$('#site-history-body').addEventListener('click', event => {
  const button = event.target.closest('.job-open');
  if (!button) return;
  overrideJobId = Number(button.dataset.id);
  reviewPage = 1;
  renderSitePage();
});

$('#confirm-rebuild-cancel').addEventListener('click', () => $('#confirm-rebuild').close());
$('#confirm-rebuild-close').addEventListener('click', () => $('#confirm-rebuild').close());
$('#confirm-rebuild-ok').addEventListener('click', () => {
  const siteKey = $('#confirm-rebuild').dataset.siteKey;
  $('#confirm-rebuild').close();
  if (siteKey) rebuildSite(siteKey);
});

document.querySelectorAll('.review-filter').forEach(button => button.addEventListener('click', () => {
  reviewFilter = button.dataset.status;
  reviewPage = 1;
  const site = siteByKey()[currentSiteKey];
  const job = site && displayJobFor(site);
  if (job) renderFindings(job.id).catch(error => toast(error.message));
}));
$('#review-prev').addEventListener('click', () => {
  const site = siteByKey()[currentSiteKey];
  const job = site && displayJobFor(site);
  if (reviewPage > 1 && job) { reviewPage -= 1; renderFindings(job.id).catch(error => toast(error.message)); }
});
$('#review-next').addEventListener('click', () => {
  const site = siteByKey()[currentSiteKey];
  const job = site && displayJobFor(site);
  if (job) { reviewPage += 1; renderFindings(job.id).catch(error => toast(error.message)); }
});

$('#task-center-btn').addEventListener('click', () => { openTaskCenter(); });
$('#rail-task-center').addEventListener('click', () => { openTaskCenter(); });
$('#task-center-close').addEventListener('click', () => $('#task-center-dialog').close());
$('#evidence-close').addEventListener('click', () => $('#evidence-dialog').close());

// 站点扫描入口按钮：切到首页总览并展开站点扫描区
$('#site-scan-btn').addEventListener('click', () => {
  navigate('overview', '/api/overview').catch(() => {});
  const section = $('#site-scan-section');
  if (section) { section.open = true; section.scrollIntoView({behavior: 'smooth', block: 'start'}); }
});

// 仪表盘刷新按钮
$('#overview-refresh').addEventListener('click', () => renderOverviewDashboard().catch(error => toast(error.message)));
// 通用视图刷新按钮
$('#generic-refresh').addEventListener('click', () => {
  const btn = $('#generic-refresh');
  const apiPath = btn.dataset.api;
  const label = btn.dataset.label;
  const description = btn.dataset.description;
  if (apiPath) renderGenericView(label, apiPath, description).catch(error => toast(error.message));
});

// ---------- 启动 ----------
api('/health').then(() => {
  $('#health').textContent = '服务正常';
  $('#health-dot').className = 'dot dot-ok';
}).catch(() => {
  $('#health').textContent = '服务异常';
  $('#health-dot').className = 'dot dot-bad';
});
poll().catch(() => {
  // 未登录或服务异常时静默；登录后 refreshAuth 会触发导航渲染
});
setInterval(() => { poll().catch(() => {}); }, 3000);


// ----- phase1 auth -----
async function refreshAuth() {
  try {
    const me = await api('/api/auth/me');
    const label = $('#auth-user');
    const loginBtn = $('#login-btn');
    const logoutBtn = $('#logout-btn');
    if (me.authenticated) {
      if (label) label.textContent = `${me.username}（${(me.roles || []).join(',') || 'user'}）`;
      if (loginBtn) loginBtn.hidden = true;
      if (logoutBtn) logoutBtn.hidden = false;
      // 角色感知：拉取 21 项导航并重新渲染侧栏
      try {
        const items = await api('/api/nav-items');
        renderNav(items);
      } catch (_) { /* 保留静态兜底 */ }
    } else {
      if (label) label.textContent = '未登录';
      if (loginBtn) loginBtn.hidden = false;
      if (logoutBtn) logoutBtn.hidden = true;
      // 未登录：保留 HTML 静态兜底，不主动清空（首屏可见结构）
      navItems = [];
    }
  } catch (_) {}
}

document.addEventListener('DOMContentLoaded', () => {
  const loginBtn = $('#login-btn');
  const logoutBtn = $('#logout-btn');
  const dialog = $('#login-dialog');
  const form = $('#login-form');
  if (loginBtn && dialog) loginBtn.addEventListener('click', () => dialog.showModal());
  const cancel = $('#login-cancel');
  if (cancel && dialog) cancel.addEventListener('click', () => dialog.close());
  if (form) form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const username = $('#login-username').value.trim();
    const password = $('#login-password').value;
    const err = $('#login-error');
    try {
      const result = await api('/api/auth/login', {method: 'POST', body: JSON.stringify({username, password})});
      csrfToken = result.csrf_token || '';
      if (err) err.hidden = true;
      dialog.close();
      await refreshAuth();
      toast('登录成功');
      // 登录后自动加载仪表盘
      navigate('overview', '/api/overview').catch(() => {});
    } catch (ex) {
      if (err) { err.hidden = false; err.textContent = ex.message || '登录失败'; }
    }
  });
  if (logoutBtn) logoutBtn.addEventListener('click', async () => {
    try {
      await api('/api/auth/logout-simple', {method: 'POST', body: '{}'});
    } catch (_) {}
    csrfToken = '';
    await refreshAuth();
    showView('overview');
  });
  refreshAuth();
});
