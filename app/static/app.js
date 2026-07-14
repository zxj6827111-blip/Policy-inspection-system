const $ = (selector) => document.querySelector(selector);
let currentJobId = null;
let reviewFilter = 'pending';
let reviewPage = 1;

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
  const minutes = Math.floor(seconds / 60);
  const remainder = seconds % 60;
  return `剩余 ${minutes} 分 ${remainder} 秒（至 ${target.toLocaleString('zh-CN', {hour12:false})}）`;
}

function toast(message) {
  const box = $('#toast'); box.textContent = message; box.style.display = 'block';
  setTimeout(() => { box.style.display = 'none'; }, 4500);
}

async function api(url, options = {}) {
  const response = await fetch(url, {headers: {'Content-Type': 'application/json'}, ...options});
  if (!response.ok) { const data = await response.json().catch(() => ({})); throw new Error(data.detail || `请求失败 ${response.status}`); }
  return response.json();
}

const statusNames = {pending:'等待启动', running:'扫描中', paused:'等待处理', partial:'本批完成', cooling:'安全冷却中', completed:'已完成', stopped:'已停止', failed:'扫描失败'};

function operationState(job) {
  if (job.status === 'running') return {hint: '正在扫描中。可暂停以保留当前位置，或停止本任务。', pause: true, resume: false, stop: true};
  if (job.status === 'pending') return {hint: job.pause_reason || '任务正在准备启动。', pause: true, resume: false, stop: true};
  if (job.status === 'cooling') return {hint: '访问风险冷却中，倒计时结束后将自动继续扫描。', pause: false, resume: false, stop: true};
  if (job.status === 'paused' && job.completion_kind === 'exception_queued') return {hint: '单条详情页异常已入队，剩余政策可继续扫描；全量结束后系统会统一复测。', pause: false, resume: true, stop: true};
  if (job.status === 'paused') return {hint: '扫描因数据校验或手动操作暂停；确认后可重新检查并恢复。', pause: false, resume: true, stop: true};
  if (job.status === 'partial') return {hint: '本批次已达到设定上限，可恢复以继续下一批。', pause: false, resume: true, stop: true};
  if (job.status === 'failed') return {hint: '扫描异常结束；可在确认原因后尝试恢复。', pause: false, resume: true, stop: true};
  return {hint: '任务已结束，可下载当前结果。', pause: false, resume: false, stop: false};
}

function renderCurrent(job) {
  currentJobId = job.id;
  $('#current-panel').hidden = false;
  $('#job-id').textContent = `#${job.id}`;
  const baselineText = job.baseline_job_id ? ` · 基准 #${job.baseline_job_id}` : '';
  $('#job-subtitle').textContent = `${JSON.parse(job.districts_json).join('、')} · ${job.mode === 'full' ? '全量' : '增量'}扫描${baselineText}`;
  $('#status').textContent = statusNames[job.status] || job.status;
  $('#status').parentElement.className = `metric-status metric-status-${job.status}`;
  $('#processed').textContent = `${job.examined_count} / ${job.estimated_total || '?'}（详情 ${job.processed_count}，复用/跳过 ${job.skipped_count}）`;
  $('#finding-count').textContent = job.finding_count;
  $('#position').textContent = `${job.current_district || '-'} / ${job.current_page || '-'}`;
  $('#current-url').textContent = job.current_url || '-';
  $('#job-message').textContent = job.pause_reason || job.last_error || '-';
  $('#cooldown').textContent = job.status === 'cooling' ? cooldownText(job.cooldown_until) : '无需冷却';
  const progress = job.estimated_total ? Math.min(100, job.examined_count / job.estimated_total * 100) : 0;
  $('#progress-bar').style.width = `${progress}%`;
  $('#export-link').href = `/api/jobs/${job.id}/export`;
  const operation = operationState(job);
  $('#operation-hint').textContent = operation.hint;
  $('#pause-btn').hidden = !operation.pause;
  $('#pause-btn').disabled = !operation.pause;
  $('#resume-btn').hidden = !operation.resume;
  $('#resume-btn').disabled = !operation.resume;
  $('#resume-btn').textContent = job.completion_kind === 'exception_queued' ? '继续扫描' : job.status === 'paused' ? '重新检查并恢复' : '继续下一批';
  $('#stop-btn').hidden = !operation.stop;
  $('#stop-btn').disabled = !operation.stop;
  if (!$('#findings-panel').contains(document.activeElement)) renderFindings(job.id).catch(error => toast(error.message));
  renderScanExceptions(job.id).catch(error => toast(error.message));
  renderItemStats(job.id).catch(error => { $('#item-stats').textContent = '统计读取失败'; });
}

async function renderItemStats(jobId) {
  const stats = await api(`/api/jobs/${jobId}/item-stats`);
  if (currentJobId !== jobId) return;
  $('#item-stats').textContent = `完整 ${stats.complete_header} · 缺字段 ${stats.incomplete_header} · 无表头 PASS ${stats.no_header_pass} · 基线复用 ${stats.baseline_reused}`;
}

async function renderScanExceptions(jobId) {
  const queue = await api(`/api/jobs/${jobId}/scan-exceptions`);
  const panel = $('#exceptions-panel');
  panel.hidden = queue.counts.review_required === 0;
  if (panel.hidden) return;
  $('#exception-counts').innerHTML = `<span>待复测 <strong>${queue.counts.pending}</strong></span><span>复测已恢复 <strong>${queue.counts.resolved}</strong></span><span>待人工复核 <strong>${queue.counts.review_required}</strong></span>`;
  const body = $('#scan-exceptions-body'); body.innerHTML = '';
  for (const exception of queue.items) {
    const card = document.createElement('article');
    card.className = 'finding-card';
    card.innerHTML = `<div class="finding-card-header"><span class="finding-district">${escapeHtml(exception.district)}</span><a href="${safeUrl(exception.url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(exception.title)}</a></div><div class="finding-content"><div><span class="finding-label">扫描位置</span><strong>第 ${exception.page_number} 页，第 ${exception.item_index + 1} 条</strong><p>${escapeHtml(exception.category)}</p></div><div><span class="finding-label">复测结果</span><p>${escapeHtml(exception.last_error)}</p><p>首次记录：${escapeHtml(new Date(exception.first_seen_at).toLocaleString('zh-CN', {hour12:false}))}</p></div></div>`;
    body.appendChild(card);
  }
}

async function renderFindings(jobId) {
  const queue = await api(`/api/jobs/${jobId}/review-queue?review_status=${reviewFilter}&page=${reviewPage}&page_size=10`);
  const panel = $('#findings-panel');
  const totalCount = Object.values(queue.counts).reduce((sum, value) => sum + value, 0);
  panel.hidden = totalCount === 0;
  if (panel.hidden) return;
  $('#review-counts').innerHTML = `<span>待复核 <strong>${queue.counts.pending}</strong></span><span>确认异常 <strong>${queue.counts.confirmed}</strong></span><span>已排除 <strong>${queue.counts.dismissed}</strong></span>`;
  document.querySelectorAll('.review-filter').forEach(button => {
    button.classList.toggle('active', button.dataset.status === reviewFilter);
  });
  const totalPages = Math.max(1, Math.ceil(queue.total / queue.page_size));
  $('#review-page-info').textContent = queue.total ? `第 ${queue.page} / ${totalPages} 页，共 ${queue.total} 条` : '当前筛选下没有问题';
  $('#review-prev').disabled = queue.page <= 1;
  $('#review-next').disabled = queue.page >= totalPages;
  const body = $('#findings-body'); body.innerHTML = '';
  for (const finding of queue.items) {
    const card = document.createElement('article');
    card.className = 'finding-card';
    card.innerHTML = `<div class="finding-card-header"><span class="finding-district">${escapeHtml(finding.district)}</span><a href="${safeUrl(finding.url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(finding.title)}</a></div><div class="finding-content"><div><span class="finding-label">问题</span><strong>${escapeHtml(finding.category)}</strong><p>${escapeHtml(finding.detail)}</p></div><div><span class="finding-label">证据</span><p>${escapeHtml(finding.evidence || '-')}</p></div></div><div class="finding-controls"><label>复核结论<select class="review-decision" data-id="${finding.id}"><option value="pending">待复核</option><option value="confirmed">确认异常</option><option value="dismissed">排除</option></select></label><label class="review-note-label">备注<input class="review-note" data-id="${finding.id}" value="${escapeHtml(finding.review_note)}" maxlength="500"></label><div class="finding-actions"><button type="button" class="evidence-open" data-id="${finding.id}">定位</button><button type="button" class="review-save" data-id="${finding.id}">保存</button></div></div>`;
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
      renderFindings(currentJobId).catch(error => toast(error.message));
    } catch (error) { toast(error.message); }
  }));
  document.querySelectorAll('.evidence-open').forEach(button => button.addEventListener('click', async () => {
    try { await openEvidence(Number(button.dataset.id)); } catch (error) { toast(error.message); }
  }));
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

async function refreshJobs() {
  const [jobs, sites] = await Promise.all([api('/api/jobs'), api('/api/scan-sites')]);
  renderSiteStatuses(sites);
  const body = $('#jobs-body'); body.innerHTML = '';
  for (const job of jobs) {
    const tr = document.createElement('tr');
    const districts = JSON.parse(job.districts_json).join('、');
    const createdAt = new Date(job.created_at).toLocaleString('zh-CN', {hour12:false});
    const baselineText = job.mode === 'incremental'
      ? (job.baseline_job_id ? `<small>基线 #${job.baseline_job_id}</small>` : '<small>旧记录无基线</small>')
      : '<small>建立基线</small>';
    tr.innerHTML = `<td data-label="任务"><button class="job-open" data-id="${job.id}">#${job.id}</button></td><td data-label="站点" class="history-source">${escapeHtml(districts)}</td><td data-label="模式" class="history-mode"><strong>${job.mode === 'full' ? '全量初始化' : '增量复查'}</strong>${baselineText}</td><td data-label="状态"><span class="status-pill status-${job.status}">${statusNames[job.status] || job.status}</span></td><td data-label="完整进度" class="history-progress-cell">${renderHistoryProgress(job)}</td><td data-label="问题">${job.finding_count}</td><td data-label="创建时间" class="history-time">${escapeHtml(createdAt)}</td><td data-label="结果"><a href="/api/jobs/${job.id}/export">Excel</a></td>`;
    body.appendChild(tr);
  }
  document.querySelectorAll('.job-open').forEach(button => button.addEventListener('click', async () => renderCurrent(await api(`/api/jobs/${button.dataset.id}`))));
  const active = jobs.find(job => ['pending','running','paused','partial','cooling','failed'].includes(job.status));
  const selected = jobs.find(job => job.id === currentJobId);
  if (selected) renderCurrent(selected);
  else if (active) renderCurrent(active);
}

function renderHistoryProgress(job) {
  const examined = Number(job.examined_count || 0);
  const total = Number(job.estimated_total || 0);
  const processed = Number(job.processed_count || 0);
  const skipped = Number(job.skipped_count || 0);
  const recorded = Number(job.scan_item_count || 0);
  const remaining = total ? Math.max(0, total - examined) : null;
  const percent = total ? Math.min(100, examined / total * 100) : 0;
  const percentText = total ? `${percent.toFixed(percent >= 10 ? 0 : 1)}%` : '待估算';
  const incomplete = job.status === 'completed' && recorded !== examined;
  const warning = incomplete ? '<em class="history-progress-warning">逐条记录不完整</em>' : '';
  return `<div class="history-progress"><div class="history-progress-head"><strong>${examined} / ${total || '?'}</strong><span>${percentText}</span></div><div class="history-progress-track" aria-label="扫描进度 ${percentText}"><span style="width:${percent}%"></span></div><small>逐条记录 ${recorded} · 详情 ${processed} · 复用/跳过 ${skipped} · 剩余 ${remaining ?? '?'}</small>${warning}</div>`;
}

function scanSiteInputs() {
  return [...document.querySelectorAll('input[name=site_key]')];
}

function syncSiteOptionStates() {
  document.querySelectorAll('.site-option').forEach(option => {
    const input = option.querySelector('input[type=checkbox]');
    option.classList.toggle('is-selected', Boolean(input?.checked));
  });
}

function renderSiteStatuses(sites) {
  for (const site of sites) {
    const runtime = $(`#site-runtime-${site.key}`);
    if (!runtime) continue;
    const job = site.latest_job;
    if (site.next_action === 'existing' && job) {
      runtime.textContent = `任务 #${job.id} · ${statusNames[job.status] || job.status} · ${job.examined_count}/${job.estimated_total || '?'}`;
      runtime.className = `site-runtime site-runtime-${job.status}`;
    } else if (site.next_action === 'resume' && job) {
      runtime.textContent = `任务 #${job.id} · ${statusNames[job.status] || job.status} · 选择后继续原任务`;
      runtime.className = `site-runtime site-runtime-${job.status}`;
    } else if (site.next_mode === 'incremental') {
      runtime.textContent = `基线 #${site.baseline_job_id} 已就绪 · 下次自动增量`;
      runtime.className = 'site-runtime site-runtime-ready';
    } else if (job && job.mode === 'full' && job.status === 'completed') {
      runtime.textContent = `任务 #${job.id} 的逐条记录不完整 · 需重新全量`;
      runtime.className = 'site-runtime site-runtime-invalid';
    } else {
      runtime.textContent = '尚未建立完整基线 · 首次自动全量';
      runtime.className = 'site-runtime site-runtime-new';
    }
  }
}

$('#scan-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const siteKeys = scanSiteInputs().filter(input => input.checked).map(input => input.value);
  if (!siteKeys.length) return toast('请至少选择一个扫描站点');
  const form = new FormData(event.target);
  try {
    const result = await api('/api/site-jobs', {method:'POST', body:JSON.stringify({site_keys:siteKeys, max_documents:Number(form.get('max_documents') || 0)})});
    currentJobId = result.jobs[0]?.job_id || null;
    scanSiteInputs().forEach(input => { input.checked = false; });
    syncSiteOptionStates();
    await refreshJobs();
    const jobNames = result.jobs.map(job => `${job.site_label} #${job.job_id}`).join('、');
    toast(`已启动：${jobNames}`);
  } catch (error) { toast(error.message); }
});

async function command(action) { if (!currentJobId) return; try { await api(`/api/jobs/${currentJobId}/${action}`, {method:'POST'}); await refreshJobs(); } catch (error) { toast(error.message); } }
$('#pause-btn').addEventListener('click', () => command('pause'));
$('#resume-btn').addEventListener('click', () => command('resume'));
$('#stop-btn').addEventListener('click', () => command('stop'));
$('#refresh-btn').addEventListener('click', refreshJobs);
$('#evidence-close').addEventListener('click', () => $('#evidence-dialog').close());
document.querySelectorAll('.review-filter').forEach(button => button.addEventListener('click', () => {
  reviewFilter = button.dataset.status;
  reviewPage = 1;
  if (currentJobId) renderFindings(currentJobId).catch(error => toast(error.message));
}));
$('#review-prev').addEventListener('click', () => {
  if (reviewPage > 1 && currentJobId) { reviewPage -= 1; renderFindings(currentJobId).catch(error => toast(error.message)); }
});
$('#review-next').addEventListener('click', () => {
  if (currentJobId) { reviewPage += 1; renderFindings(currentJobId).catch(error => toast(error.message)); }
});
scanSiteInputs().forEach(input => input.addEventListener('change', syncSiteOptionStates));
$('#history-details').addEventListener('toggle', (event) => {
  const hint = event.currentTarget.querySelector('.collapse-hint');
  hint.textContent = event.currentTarget.open ? hint.dataset.expanded : hint.dataset.collapsed;
  if (event.currentTarget.open) refreshJobs().catch(error => toast(error.message));
});

api('/health').then(() => { $('#health').textContent = '本地服务正常'; }).catch(() => { $('#health').textContent = '服务异常'; });
refreshJobs().catch(error => toast(error.message));
setInterval(() => refreshJobs().catch(() => {}), 3000);
