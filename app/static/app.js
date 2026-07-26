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
async function api(url, options = {}) {
  const response = await fetch(url, {headers: {'Content-Type': 'application/json'}, ...options});
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

const siteByKey = () => Object.fromEntries(sites.map(site => [site.key, site]));
const signature = labels => (labels || []).slice().sort().join('');
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

// ---------- 侧栏 ----------
function renderSidebar() {
  let alerts = {district: 0, municipal: 0};
  for (const site of sites) {
    const state = siteState(site);
    const dot = $(`#side-dot-${site.key}`);
    const sub = $(`#side-sub-${site.key}`);
    if (dot) dot.className = `s-dot ${state.dot}`;
    if (sub) sub.textContent = state.sub;
    if (state.code === 'attention' || state.code === 'invalid') {
      alerts[site.source_level === '区级' ? 'district' : 'municipal'] += 1;
    }
  }
  for (const group of ['district', 'municipal']) {
    const badge = $(`#alert-${group}`);
    if (!badge) continue;
    badge.hidden = alerts[group] === 0;
    badge.textContent = `⚠ ${alerts[group]}`;
  }
  document.querySelectorAll('.side-item').forEach(item => {
    const active = view === 'site' ? item.dataset.site === currentSiteKey : item.dataset.nav === 'overview';
    item.classList.toggle('active', active);
  });
  const activeItem = document.querySelector('.side-item.active');
  const group = activeItem && activeItem.closest('details.side-group');
  if (group) group.open = true;
}

// ---------- 总览 ----------
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
      <td class="num">${fmtNum(job.finding_count)}</td>
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
  if (job.status === 'partial') title = '本批次已完成设定上限';
  if (job.status === 'failed') title = '扫描异常结束';
  $('#callout-title').textContent = title;
  $('#callout-desc').textContent = message;
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

function renderStats(site, job) {
  const row = $('#stat-row');
  if (!job) { row.hidden = true; return; }
  row.hidden = false;
  let gen = {};
  try { gen = JSON.parse(job.generation_stats_json || '{}'); } catch { gen = {}; }
  const discovered = gen.discovered ?? job.estimated_total;
  const added = gen.new ?? 0;
  const reused = gen.reused ?? job.skipped_count;
  row.innerHTML = `
    <div class="stat"><span class="label">发现条目</span><div class="value num">${discovered != null ? fmtNum(discovered) : '?'}</div></div>
    <div class="stat"><span class="label">新增</span><div class="value num">${fmtNum(added)}</div></div>
    <div class="stat"><span class="label">基线复用</span><div class="value num">${fmtNum(reused)}</div><div class="hint">未开详情直接复用</div></div>
    <div class="stat"><span class="label">表头完整</span><div class="value num" id="stat-header">…</div><div class="hint" id="stat-header-hint">读取中</div></div>
    <div class="stat"><span class="label">发现问题</span><div class="value num link" id="stat-findings">${fmtNum(job.finding_count)}</div><div class="hint">点击查看复核 ↓</div></div>`;
  const findingsTile = $('#stat-findings');
  if (findingsTile) findingsTile.addEventListener('click', () => {
    const panel = $('#findings-panel');
    if (!panel.hidden) panel.scrollIntoView({behavior: 'smooth'});
  });
  api(`/api/jobs/${job.id}/item-stats`).then(stats => {
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
  $('#rail-findings-card').hidden = !job || !Number(job.finding_count);
  if (job) $('#rail-findings-num').textContent = fmtNum(job.finding_count);
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
      <td class="num">${fmtNum(job.finding_count)}</td>
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
  $('#evidence-source').href = validatedUrl(finding.url);
  $('#evidence-page-value').textContent = finding.page_value || '-';
  $('#evidence-body-value').textContent = finding.body_value || '-';
  $('#evidence-rule-text').textContent = finding.evidence || finding.detail || '-';
  $('#evidence-body').innerHTML = highlightEvidence(finding.body_text, [finding.page_value, finding.body_value]);
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
  refreshUi();
  window.scrollTo({top: 0});
}

function refreshUi() {
  renderSidebar();
  if (view === 'overview') renderOverview();
  else renderSitePage();
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
document.querySelector('.sidebar').addEventListener('click', event => {
  const item = event.target.closest('.side-item');
  if (!item) return;
  if (item.dataset.nav === 'overview') showView('overview');
  else if (item.dataset.site) showView('site', item.dataset.site);
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

// ---------- 启动 ----------
api('/health').then(() => {
  $('#health').textContent = '服务正常';
  $('#health-dot').className = 'dot dot-ok';
}).catch(() => {
  $('#health').textContent = '服务异常';
  $('#health-dot').className = 'dot dot-bad';
});
poll().catch(error => toast(error.message));
setInterval(() => poll().catch(() => {}), 3000);
